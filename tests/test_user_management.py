"""Settings user management: administrators add accounts and reset passwords."""
from contextlib import contextmanager
import json

import pytest
from fastapi.testclient import TestClient

from app import api, auth
from app.auth import manage
from routers import CONFIG_MODULES, patch_all


@pytest.fixture
def settings_file(tmp_path, monkeypatch, auth_memory):
    path = tmp_path / 'local.json'
    path.write_text(json.dumps({
        'host': '0.0.0.0', 'browser_user': 'admin',
        'browser_password_hash': auth.password_hash('fixture-admin'),
        'auth_secret': 'fixture-secret-' * 4, 'database': {'password': 'kept'},
        'browser_accounts': {'guest': {'role': 'guest', 'password_hash': auth.password_hash('guest-password')},
                             'reader': {'role': 'regular', 'password_hash': auth.password_hash('reader-password')}}}),
        encoding='utf-8')
    patch_all(monkeypatch, CONFIG_MODULES, 'load_config', lambda: json.loads(path.read_text(encoding='utf-8')))
    monkeypatch.setattr(manage, 'config_path', lambda: path)
    return path


@contextmanager
def signed_in(username, password):
    # Not `with TestClient(...)`: that would start the app lifespan and its database.
    client = TestClient(api.app, base_url='https://testserver')
    try:
        assert client.post('/api/login', json={'username': username, 'password': password}).status_code == 200
        post = client.post
        def confirmed_post(url, **kwargs):
            if url in ('/api/auth/users', '/api/auth/users/password') and 'json' in kwargs:
                kwargs['json'] = {'admin_password': password, **kwargs['json']}
            return post(url, **kwargs)
        client.post = confirmed_post
        yield client
    finally:
        client.close()


def test_only_administrators_manage_users(settings_file):
    before = settings_file.read_bytes()
    for name, password in [('guest', 'guest-password'), ('reader', 'reader-password')]:
        with signed_in(name, password) as client:
            assert client.get('/api/auth/users').status_code == 403
            assert client.post('/api/auth/users', json={'username': 'x1', 'password': 'long-enough', 'role': 'admin'}).status_code == 403
            assert client.post('/api/auth/users/password', json={'username': 'admin', 'password': 'long-enough'}).status_code == 403
    anonymous = TestClient(api.app, base_url='https://testserver')
    assert anonymous.get('/api/auth/users').status_code == 401
    anonymous.close()
    assert settings_file.read_bytes() == before


def test_admin_lists_and_creates_users_with_hashed_passwords(settings_file):
    with signed_in('admin', 'fixture-admin') as admin:
        users = admin.get('/api/auth/users').json()['users']
        assert users == [{'username': 'admin', 'role': 'admin', 'primary': True},
                         {'username': 'guest', 'role': 'guest', 'primary': False},
                         {'username': 'reader', 'role': 'regular', 'primary': False}]
        response = admin.post('/api/auth/users', json={'username': 'meilin', 'password': 'Generated-Pass-1', 'role': 'regular'})
        assert response.status_code == 200
        assert {'username': 'meilin', 'role': 'regular', 'primary': False} in response.json()['users']
    saved = json.loads(settings_file.read_text(encoding='utf-8'))
    assert 'Generated-Pass-1' not in settings_file.read_text(encoding='utf-8')
    assert auth.HASH_PATTERN.fullmatch(saved['browser_accounts']['meilin']['password_hash'])
    assert saved['database'] == {'password': 'kept'}
    with signed_in('meilin', 'Generated-Pass-1') as client:
        assert client.get('/api/me').json()['role'] == 'regular'


@pytest.mark.parametrize('payload,message', [
    ({'username': 'Reader', 'password': 'long-enough', 'role': 'guest'}, 'already exists'),
    ({'username': 'admin', 'password': 'long-enough', 'role': 'guest'}, 'already exists'),
    ({'username': '-bad', 'password': 'long-enough', 'role': 'guest'}, 'Username'),
    ({'username': 'has space', 'password': 'long-enough', 'role': 'guest'}, 'Username'),
    ({'username': 'ok', 'password': 'short', 'role': 'guest'}, 'between 8'),
    ({'username': 'ok', 'password': ' padded-password ', 'role': 'guest'}, 'space'),
    ({'username': 'ok', 'password': 'long-enough', 'role': 'owner'}, 'role'),
    ({'username': 'ok', 'password': 12345678, 'role': 'guest'}, 'between 8'),
])
def test_invalid_new_accounts_change_nothing(settings_file, payload, message):
    before = settings_file.read_bytes()
    with signed_in('admin', 'fixture-admin') as admin:
        response = admin.post('/api/auth/users', json=payload)
        assert response.status_code == 422 and message in response.text
    assert settings_file.read_bytes() == before


def test_reset_signs_the_user_out_and_replaces_the_password(settings_file):
    with signed_in('admin', 'fixture-admin') as admin, signed_in('reader', 'reader-password') as reader:
        response = admin.post('/api/auth/users/password', json={'username': 'reader', 'password': 'Brand-New-Pass'})
        assert response.status_code == 200 and response.json()['signed_out'] is False
        assert reader.get('/api/me').status_code == 401
        assert admin.get('/api/me').status_code == 200
        assert admin.post('/api/auth/users/password', json={'username': 'nobody', 'password': 'Brand-New-Pass'}).status_code == 422
    accounts = auth.accounts(json.loads(settings_file.read_text(encoding='utf-8')))
    assert not auth.verify_password(accounts['reader'], 'reader-password')
    assert auth.verify_password(accounts['reader'], 'Brand-New-Pass')
    with signed_in('reader', 'Brand-New-Pass'):
        pass


def test_resetting_own_password_signs_out_and_main_admin_hash_changes(settings_file):
    with signed_in('admin', 'fixture-admin') as admin:
        response = admin.post('/api/auth/users/password', json={'username': 'admin', 'password': 'Admin-New-Pass'})
        assert response.status_code == 200 and response.json()['signed_out'] is True
        assert admin.get('/api/me').status_code == 401
    saved = json.loads(settings_file.read_text(encoding='utf-8'))
    assert auth.verify_password({'password_hash': saved['browser_password_hash']}, 'Admin-New-Pass')
    assert 'admin' not in saved['browser_accounts']
    with signed_in('admin', 'Admin-New-Pass'):
        pass


def test_cross_origin_commands_are_refused(settings_file):
    before = settings_file.read_bytes()
    with signed_in('admin', 'fixture-admin') as admin:
        response = admin.post('/api/auth/users', headers={'origin': 'https://evil.test'},
                              json={'username': 'intruder', 'password': 'long-enough', 'role': 'admin'})
        assert response.status_code == 403
    assert settings_file.read_bytes() == before


def test_concurrent_settings_edit_is_not_overwritten(settings_file):
    def change(config):
        settings_file.write_text(json.dumps({**config, 'other': 'edited elsewhere'}), encoding='utf-8')
        return manage.with_new_account(config, 'late', 'long-enough', 'guest')
    with pytest.raises(RuntimeError, match='Settings changed'):
        manage.update_config(change)
    saved = json.loads(settings_file.read_text(encoding='utf-8'))
    assert saved['other'] == 'edited elsewhere' and 'late' not in saved['browser_accounts']
    assert sorted(p.name for p in settings_file.parent.iterdir()) == ['local.json', 'local.json.lock']


def _create_in_process(path, username, start):
    from app.auth import manage as worker_manage
    start.wait()
    worker_manage.update_config(lambda config: worker_manage.with_new_account(config, username, 'long-enough', 'guest'), path)


def _hold_lock(path, held, release):
    from app.config_lock import config_write_lock
    with config_write_lock(path):
        held.set()
        release.wait(30)


def test_concurrent_processes_never_lose_an_account(settings_file):
    import multiprocessing
    context = multiprocessing.get_context('spawn')
    start = context.Event()
    names = [f'parallel{i}' for i in range(4)]
    workers = [context.Process(target=_create_in_process, args=(str(settings_file), name, start)) for name in names]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(60)
    assert [worker.exitcode for worker in workers] == [0, 0, 0, 0]
    saved = json.loads(settings_file.read_text(encoding='utf-8'))
    assert set(names) <= set(saved['browser_accounts'])
    assert not [p.name for p in settings_file.parent.iterdir() if p.name.startswith('.accounts-')]


def test_settings_lock_excludes_another_process(settings_file):
    import multiprocessing
    from app.config_lock import config_write_lock
    context = multiprocessing.get_context('spawn')
    held, release = context.Event(), context.Event()
    holder = context.Process(target=_hold_lock, args=(str(settings_file), held, release))
    holder.start()
    try:
        assert held.wait(30)
        with pytest.raises(RuntimeError, match='another process'):
            with config_write_lock(settings_file, timeout=0.3):
                pass
    finally:
        release.set()
        holder.join(30)
    with config_write_lock(settings_file, timeout=5):
        pass


def test_storage_save_holds_the_same_lock():
    import inspect
    from app.storage import config as storage_config
    source = inspect.getsource(storage_config.save)
    assert 'with config_write_lock(CONFIG_PATH):' in source


def test_lock_file_is_never_captured_by_backups():
    from app.backup.policy import classify_file
    assert classify_file('config', 'local.json.lock').category != 'authoritative'


@pytest.mark.parametrize('endpoint,payload', [
    ('/api/auth/users', {'username': 'newuser', 'password': 'new-password', 'role': 'regular'}),
    ('/api/auth/users/password', {'username': 'reader', 'password': 'new-password'}),
])
@pytest.mark.parametrize('confirmation', [None, '', 'wrong-password', 42])
def test_account_changes_require_current_admin_password(settings_file, endpoint, payload, confirmation):
    before = settings_file.read_bytes()
    with signed_in('admin', 'fixture-admin') as client:
        result = client.post(endpoint, json={**payload, 'admin_password': confirmation})
        assert result.status_code == 403
    assert settings_file.read_bytes() == before


def test_confirmation_uses_signed_in_admin_not_target_password(settings_file):
    with signed_in('admin', 'fixture-admin') as client:
        assert client.post('/api/auth/users', json={
            'username': 'second-admin', 'password': 'second-password', 'role': 'admin'}).status_code == 200
    with signed_in('second-admin', 'second-password') as client:
        assert client.post('/api/auth/users/password', json={
            'username': 'reader', 'password': 'changed-password', 'admin_password': 'fixture-admin'}).status_code == 403
        assert client.post('/api/auth/users/password', json={
            'username': 'reader', 'password': 'changed-password'}).status_code == 200


def test_confirmation_is_rate_limited(settings_file, auth_memory):
    before = settings_file.read_bytes()
    with signed_in('admin', 'fixture-admin') as client:
        auth_memory.retry = 60
        result = client.post('/api/auth/users', json={
            'username': 'newuser', 'password': 'new-password', 'role': 'regular'})
        assert result.status_code == 429 and result.headers['Retry-After'] == '60'
    assert settings_file.read_bytes() == before
