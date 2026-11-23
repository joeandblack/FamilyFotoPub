"""HTTPS compatibility checks without sockets, certificates or live configuration."""
import ssl

import pytest
from fastapi.testclient import TestClient

from app.service.server import server_options
from app.auth import password_hash
pytestmark = pytest.mark.usefixtures('auth_memory')


def test_plain_http_is_refused_when_tls_is_not_configured():
    for config in ({}, {'host': '0.0.0.0'}, {'tls': None}):
        with pytest.raises(ValueError, match='HTTPS is required'):
            server_options(config)


def test_development_switch_allows_plain_http_on_this_computer_only(monkeypatch, capsys):
    monkeypatch.setenv('FAMILYFOTO_DEV_HTTP', '1')
    options = server_options({})
    assert options['host'] == '127.0.0.1' and 'ssl_certfile' not in options and options['proxy_headers'] is False
    assert 'WARNING' in capsys.readouterr().out
    for host in ('0.0.0.0', '192.168.1.5'):
        with pytest.raises(ValueError, match='this computer only'):
            server_options({'host': host})
    # The switch never weakens a configured certificate: a broken one still fails closed.
    with pytest.raises(ValueError):
        server_options({'tls': {'certfile': 'missing', 'keyfile': 'missing'}})
    monkeypatch.setenv('FAMILYFOTO_DEV_HTTP', 'true')
    with pytest.raises(ValueError, match='HTTPS is required'):
        server_options({})


def test_tls_paths_and_protocol(tmp_path, monkeypatch):
    monkeypatch.setattr('app.service.server.ROOT', tmp_path)
    # These are placeholders: no cryptographic material is generated or read.
    (tmp_path / 'cert.pem').touch()
    (tmp_path / 'key.pem').touch()
    options = server_options({'tls': {'certfile': 'cert.pem', 'keyfile': 'key.pem'}})
    assert options['ssl_certfile'] == str(tmp_path / 'cert.pem')
    assert options['ssl_keyfile'] == str(tmp_path / 'key.pem')
    assert options['proxy_headers'] is False
    assert options['ssl_version'] == ssl.PROTOCOL_TLS_SERVER
    assert ssl.SSLContext(options['ssl_version']).minimum_version >= ssl.TLSVersion.TLSv1_2


@pytest.mark.parametrize('tls', [False, '', {}, {'certfile': 'missing'},
                                      {'certfile': 'missing', 'keyfile': 'missing'}])
def test_incomplete_tls_fails_instead_of_http(tls):
    with pytest.raises(ValueError):
        server_options({'tls': tls})


@pytest.fixture
def isolated_app(monkeypatch):
    from app import api
    from routers import CONFIG_MODULES, patch_all
    config = {'host': '127.0.0.1', 'browser_user': 'fixture',
              'browser_password_hash': password_hash('test-only-password'), 'auth_secret':'fixture-secret-'*4,
              'browser_accounts': {'reader': {'role': 'regular', 'password_hash': password_hash('test-reader')}}}
    patch_all(monkeypatch, CONFIG_MODULES, 'load_config', lambda: config)
    # No TestClient context manager: no application lifespan/migrations/dispatcher.
    return api.app


@pytest.mark.parametrize('scheme', ['http', 'https'])
def test_login_cookie_tracks_actual_transport(isolated_app, scheme):
    with_client = TestClient(isolated_app, base_url=f'{scheme}://testserver')
    try:
        response = with_client.post('/api/login', json={
            'username': 'reader', 'password': 'test-reader'}, headers={
            'origin': f'{scheme}://testserver',
            'x-forwarded-proto': 'http' if scheme == 'https' else 'https',
            'forwarded': 'proto=https;host=untrusted'})
        assert response.status_code == 200
        cookie = response.headers['set-cookie']
        assert ('; Secure' in cookie) == (scheme == 'https')
        assert 'HttpOnly' in cookie and 'SameSite=strict' in cookie
        assert with_client.get('/api/me').json()['role'] == 'regular'
        if scheme == 'https':
            assert with_client.get('http://testserver/api/me').status_code == 401
    finally:
        with_client.close()


def test_https_origin_and_unauthenticated_access(isolated_app):
    client = TestClient(isolated_app, base_url='https://testserver')
    try:
        assert client.get('/api/me').status_code == 401
        response = client.post('/api/login', json={
            'username': 'fixture', 'password': 'test-only-password'},
            headers={'origin': 'http://testserver'})
        assert response.status_code == 403
        assert 'set-cookie' not in response.headers
    finally:
        client.close()


def test_session_epoch_revokes_old_cookies_without_password_change():
    import time
    from app.auth import session_token, session_user
    config = {'browser_user': 'admin', 'browser_password_hash': password_hash('fixture'),'auth_secret':'fixture-secret-'*4}
    original_hash = config['browser_password_hash']
    old = session_token(config, int(time.time()) + 60)
    assert session_user(config, old)['role'] == 'admin'
    config['session_epoch'] = 'approved-transition-fixture'
    assert session_user(config, old) is None
    fresh = session_token(config, int(time.time()) + 60)
    assert session_user(config, fresh)['role'] == 'admin'
    assert config['browser_password_hash'] == original_hash
