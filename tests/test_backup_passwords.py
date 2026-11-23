"""Backup passwords (app.backup.passwords): the administrator password protects each backup."""
import io
import json
import os
from pathlib import Path
import pickle
import struct
import subprocess
import sys

import pytest

from app.auth import password_hash
from app.backup import password_handoff, passwords
from app.backup.passwords import BackupPassword

ROOT = Path(__file__).resolve().parents[1]
SALT = bytes(range(16))  # Fixed salts only in tests.


# The one scrypt profile, pinned: the same password and salt give this key on every platform.

@pytest.mark.real_scrypt
@pytest.mark.parametrize('text,expected', [
    ('correct horse battery staple', 'GylG2nH0EXnoO5ncM4QtFXQbh8QSHIx_N4HB34ZPtYs='),
    (' Grüße, 東京 ', 'k6LjxIWHx3wHIrGZkjRVYybprkV--A4wS5wGeFYo7dI='),
    # The same text decomposed (u + combining diaeresis): NFC makes it the same password.
    (' Grüße, 東京 ', 'k6LjxIWHx3wHIrGZkjRVYybprkV--A4wS5wGeFYo7dI='),
])
def test_scrypt_profile_is_fixed(text, expected):
    assert passwords.derive(BackupPassword(text), SALT).decode() == expected


@pytest.mark.real_scrypt
def test_real_round_trip_and_wrong_password(tmp_path):
    key = passwords.create_context(tmp_path, BackupPassword('correct horse battery staple'))
    assert passwords.unlock_context(tmp_path, BackupPassword('correct horse battery staple')) == key
    with pytest.raises(passwords.WrongPassword, match='incorrect'):
        passwords.unlock_context(tmp_path, BackupPassword('correct horse battery stapler'))


def test_different_salts_and_spaces_give_different_keys():
    password = BackupPassword('correct horse battery staple')
    assert passwords.derive(password, SALT) != passwords.derive(password, bytes(16))
    assert passwords.derive(password, SALT) != passwords.derive(BackupPassword('correct horse battery staple '), SALT)


@pytest.mark.parametrize('text', ['a\nb' + 'x' * 12, 'a\rb' + 'x' * 12, 'a\0b' + 'x' * 12, ''])
def test_line_breaks_nul_and_empty_are_refused(text):
    with pytest.raises(passwords.UnsuitablePassword):
        BackupPassword(text)


def test_size_limits_count_bytes_after_normalization():
    BackupPassword('é' * 512)  # 1024 UTF-8 bytes
    with pytest.raises(passwords.UnsuitablePassword, match='1024 bytes'):
        BackupPassword('é' * 512 + 'x')
    with pytest.raises(passwords.UnsuitablePassword, match='8 characters'):
        BackupPassword('seven77', new=True)
    BackupPassword('eight888', new=True)
    BackupPassword('seven77')  # A restore applies no minimum.


def test_secrets_are_redacted_and_never_pickled():
    password = BackupPassword('correct horse battery staple')
    key = passwords.new_key(password)
    for value in (password, key):
        assert 'correct' not in repr(value) and 'correct' not in str(value)
        with pytest.raises(TypeError):
            pickle.dumps(value)
    assert key.decode() not in repr(key)


# The public context file.

def context(tmp_path):
    passwords.create_context(tmp_path, BackupPassword('correct horse battery staple'))
    return json.loads((tmp_path / passwords.FILE).read_text())


def write(tmp_path, raw):
    (tmp_path / passwords.FILE).write_bytes(raw if isinstance(raw, bytes) else json.dumps(raw).encode())


@pytest.mark.parametrize('change', [
    lambda value: {**value, 'extra': 1},
    lambda value: {key: item for key, item in value.items() if key != 'check'},
    lambda value: {**value, 'format': 'familyfoto-password-v2'},
    lambda value: {**value, 'backup_id': value['backup_id'].upper()},
    lambda value: {**value, 'salt': value['salt'] + '='},
    lambda value: {**value, 'salt': value['salt'][:-1] + ('B' if value['salt'][-1] != 'B' else 'C')},
])
def test_odd_context_files_are_unsupported_before_any_kdf(tmp_path, monkeypatch, change):
    value = change(context(tmp_path))
    write(tmp_path, value)
    monkeypatch.setattr(passwords, 'derive', lambda *args: pytest.fail('KDF ran on an unsupported file'))
    with pytest.raises(passwords.UnsupportedBackup, match='not supported'):
        passwords.unlock_context(tmp_path, BackupPassword('correct horse battery staple'))


def test_duplicate_fields_huge_and_missing_files_are_unsupported(tmp_path):
    value = context(tmp_path)
    raw = json.dumps(value)[:-1] + ', "salt": "' + value['salt'] + '"}'
    write(tmp_path, raw.encode())
    with pytest.raises(passwords.UnsupportedBackup):
        passwords.read_context(tmp_path)
    write(tmp_path, b' ' * (passwords.FILE_LIMIT + 1))
    with pytest.raises(passwords.UnsupportedBackup):
        passwords.read_context(tmp_path)
    (tmp_path / passwords.FILE).unlink()
    with pytest.raises(passwords.UnsupportedBackup, match='Create a new backup'):
        passwords.read_context(tmp_path)


def test_a_damaged_check_or_salt_reads_as_wrong_password(tmp_path):
    value = context(tmp_path)
    write(tmp_path, {**value, 'check': value['check'][:20] + ('A' if value['check'][20] != 'A' else 'B') + value['check'][21:]})
    with pytest.raises(passwords.WrongPassword):
        passwords.unlock_context(tmp_path, BackupPassword('correct horse battery staple'))
    # A valid token for another context's text doesn't pass either.
    other = passwords.new_key(BackupPassword('correct horse battery staple'))
    write(tmp_path, {**value, 'check': other.context['check']})
    with pytest.raises(passwords.WrongPassword):
        passwords.unlock_context(tmp_path, BackupPassword('correct horse battery staple'))


def test_publish_never_replaces_another_context(tmp_path):
    key = passwords.create_context(tmp_path, BackupPassword('correct horse battery staple'))
    before = (tmp_path / passwords.FILE).read_bytes()
    passwords.publish(tmp_path, key)  # The same context again: a retry.
    with pytest.raises(passwords.PasswordProblem, match='another backup password'):
        passwords.publish(tmp_path, passwords.new_key(BackupPassword('correct horse battery staple')))
    assert (tmp_path / passwords.FILE).read_bytes() == before


def test_binding_names_one_context():
    key = passwords.new_key(BackupPassword('correct horse battery staple'))
    passwords.validate_context_binding(key, key.binding())
    other = passwords.new_key(BackupPassword('correct horse battery staple'))
    for binding in (other.binding(), None, {**key.binding(), 'salt': other.context['salt']}):
        with pytest.raises(passwords.UnsupportedBackup):
            passwords.validate_context_binding(key, binding)
    with pytest.raises(passwords.UnsupportedBackup):
        passwords.validate_context_binding(bytes(key), key.binding())


# The administrator password.

def settings(tmp_path, password='correct horse battery staple', **extra):
    (tmp_path / 'local.json').write_text(json.dumps({'browser_user': 'family', 'browser_password_hash': password_hash(password),
                                                     **extra}))
    return tmp_path


def test_a_new_backup_takes_only_the_administrator_password(tmp_path):
    root = settings(tmp_path, browser_accounts={'other': {'role': 'admin', 'password_hash': password_hash('other admin password')}})
    assert passwords.administrator_password(root, 'correct horse battery staple') == BackupPassword('correct horse battery staple')
    for wrong in ('correct horse battery stapl', 'other admin password'):
        with pytest.raises(passwords.NotAdministratorPassword, match='not the FamilyFoto administrator password'):
            passwords.administrator_password(root, wrong)


def test_a_short_administrator_password_cannot_protect_a_backup(tmp_path):
    root = settings(tmp_path, 'seven77')
    with pytest.raises(passwords.UnsuitablePassword, match='too short to protect a backup'):
        passwords.administrator_password(root, 'seven77')


@pytest.mark.real_scrypt
def test_eight_character_administrator_password_round_trip(tmp_path):
    root = settings(tmp_path, 'eight888')
    password = passwords.administrator_password(root, 'eight888')
    (tmp_path / 'backup').mkdir()
    key = passwords.create_context(tmp_path / 'backup', password)
    assert passwords.unlock_context(tmp_path / 'backup', BackupPassword('eight888')) == key
    with pytest.raises(passwords.WrongPassword):
        passwords.unlock_context(tmp_path / 'backup', BackupPassword('wrong888'))


# Input.

def test_frames_round_trip_and_refuse_bad_ones():
    text = ' Grüße "quoted" \\ back '
    assert passwords.read_frame(io.BytesIO(passwords.frame(text))) == text
    good = passwords.frame(text)
    for bad in (good[:-1], struct.pack('>I', passwords.FRAME_LIMIT + 1), struct.pack('>I', 1) + b'{',
                struct.pack('>I', 30) + b'{"password": "a", "password": "b"}'[:30],
                struct.pack('>I', 19) + b'{"other": "abcdefg"}'[:19]):
        with pytest.raises(passwords.UnsuitablePassword):
            passwords.read_frame(io.BytesIO(bad))


def test_a_line_loses_only_its_line_ending():
    assert passwords.read_line(io.BytesIO(b'  spaced password  \r\n')) == '  spaced password  '
    assert passwords.read_line(io.BytesIO(b'tab\tinside password\n')) == 'tab\tinside password'
    with pytest.raises(passwords.UnsuitablePassword):
        passwords.read_line(io.BytesIO(b'two\nlines of password'))


# Handing a password to a worker process.

CHILD = r'''
import subprocess, sys
sys.path.insert(0, sys.argv[1])
from app.backup import password_handoff, passwords
text = password_handoff.receive()
if text != 'right password here':
    password_handoff.refuse(passwords.WrongPassword())
    raise SystemExit(1)
password_handoff.ready()
# A grandchild that tries to read stdin gets nothing: the password pipe was the child's alone.
seen = subprocess.run([sys.executable, '-c', 'import sys; print(len(sys.stdin.buffer.read()))'],
                      capture_output=True, text=True).stdout.strip()
print('grandchild read', seen, file=sys.stderr)
'''


def test_handoff_waits_for_ready_and_reports_a_refusal(tmp_path):
    script = tmp_path / 'child.py'
    script.write_text(CHILD)
    log = tmp_path / 'log.txt'
    with log.open('wb') as output:
        process = password_handoff.launch([sys.executable, str(script), str(ROOT)], 'right password here',
                                          cwd=tmp_path, log=output)
        process.wait(30)
    assert 'grandchild read 0' in log.read_text()
    assert 'right password here' not in log.read_text()
    with log.open('wb') as output:
        with pytest.raises(password_handoff.HandoffFailed, match='incorrect') as caught:
            password_handoff.launch([sys.executable, str(script), str(ROOT)], 'wrong password here', cwd=tmp_path, log=output)
    assert caught.value.reason == 'wrong-password'


def test_handoff_gives_up_on_a_child_that_never_answers(tmp_path):
    with (tmp_path / 'log.txt').open('wb') as output:
        with pytest.raises(password_handoff.HandoffFailed, match='did not confirm'):
            password_handoff.launch([sys.executable, '-c', 'import time; time.sleep(30)'], 'some password here',
                                    cwd=tmp_path, log=output, timeout=2)


# The setup windows' pre-Start check (installer/password_check.py) and the CLI.

def backup_folder(tmp_path, password='correct horse battery staple'):
    from app.backup.encrypted import seal_stream
    folder = tmp_path / 'backup'
    folder.mkdir()
    key = passwords.create_context(folder, BackupPassword(password))
    seal_stream(io.BytesIO(b'{}'), folder / 'library', key, estimated_bytes=2)
    return folder, key


def check(folder, text):
    result = subprocess.run([sys.executable, '-I', '-B', str(ROOT / 'installer/password_check.py'), '--backup', str(folder)],
                            input=passwords.frame(text), capture_output=True, timeout=60)
    return result.returncode, json.loads(result.stdout.decode().strip().splitlines()[-1]), result


@pytest.mark.real_scrypt
def test_setup_check_answers_without_the_password_in_its_output(tmp_path):
    folder, key = backup_folder(tmp_path)
    code, answer, result = check(folder, 'correct horse battery staple')
    assert code == 0 and answer == {'ok': True, 'backup_id': key.backup_id}
    code, answer, result = check(folder, 'Correct horse battery staple')
    assert code == 1 and answer['reason'] == 'wrong-password'
    assert b'orse battery' not in result.stdout + result.stderr
    (folder / passwords.FILE).unlink()
    assert check(folder, 'correct horse battery staple')[1]['reason'] == 'unsupported-backup'


@pytest.mark.real_scrypt
def test_cli_refuses_a_wrong_password_without_repeating_it(tmp_path):
    folder, _ = backup_folder(tmp_path)
    result = subprocess.run([sys.executable, '-m', 'app.backup.manage', 'verify-library', '--directory', str(folder),
                             '--backup-password-stdin'], input=b'Wrong horse battery staple\n', capture_output=True,
                            cwd=ROOT, timeout=60, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    report = json.loads(result.stdout.decode().strip().splitlines()[-1])
    assert result.returncode == 1 and report['reason'] == 'wrong-password'
    assert b'orse battery' not in result.stdout + result.stderr


def test_validation_errors_never_echo_the_submitted_body():
    from fastapi import Body, FastAPI
    from fastapi.exceptions import RequestValidationError
    from fastapi.testclient import TestClient
    from app.api import validation_failed
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_failed)

    @app.post('/x')
    def route(payload: dict = Body(...)):
        return payload
    response = TestClient(app).post('/x', json='a secret password value')
    assert response.status_code == 422 and 'secret password' not in response.text
