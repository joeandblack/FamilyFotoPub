"""Credential upgrade, revocation, and login admission regressions."""
from concurrent.futures import ThreadPoolExecutor
import asyncio
import json
import threading
import time
from unittest.mock import patch

import pytest
import httpx
import psycopg
from fastapi.testclient import TestClient

from app import api, auth, database
from app.database import schema_upgrading
from app.auth import api as auth_api
from routers import CONFIG_MODULES, DATA_MODULES, patch_all
from app.auth.store import SessionStore
from scripts.upgrade_auth import upgrade, upgraded_config
from test_database_runtime import isolated_db


@pytest.fixture
def settings():
    return {'host':'0.0.0.0','browser_user':'admin',
            'browser_password_hash':auth.password_hash('fixture'),
            'auth_secret':'fixture-secret-'*4,
            'browser_accounts':{'guest':{'role':'guest','password_hash':auth.password_hash('guest-password')}}}


def test_plaintext_is_rejected_not_used_as_fallback(settings):
    assert auth.verify_password({'password':'secret'},'secret') is False
    with pytest.raises(ValueError,match='Plaintext'):
        auth.accounts({**settings,'browser_password':'secret'})
    settings['browser_accounts']['guest']['password']='secret'
    with pytest.raises(ValueError,match='only salted'):
        auth.accounts(settings)
    assert not auth.verify_password({'password_hash':'pbkdf2_sha256$9999999999$00$00'},'x')
    assert not auth.verify_password({'password_hash':settings['browser_password_hash']},'x'*1025)


def test_upgrade_preserves_passwords_and_unrelated_settings(tmp_path):
    path=tmp_path/'local.json'
    old={'browser_user':'admin','browser_password':'existing-secret',
         'browser_accounts':{'guest':{'role':'guest','password':'guest-secret'}},
         'database':{'password':'database-secret'},'other':{'keep':True}}
    path.write_text(json.dumps(old),encoding='utf-8')
    assert upgrade(path)
    result=json.loads(path.read_text())
    assert 'browser_password' not in result
    assert 'password' not in result['browser_accounts']['guest']
    assert auth.verify_password(auth.accounts(result)['admin'],'existing-secret')
    assert auth.verify_password(auth.accounts(result)['guest'],'guest-secret')
    assert result['database']==old['database'] and result['other']==old['other']
    assert not upgrade(path)
    assert len(list(tmp_path.iterdir()))==1


def test_upgrade_conflicting_hash_does_not_overwrite(tmp_path,settings):
    path=tmp_path/'local.json'
    path.write_text(json.dumps({**settings,'browser_password':'not-the-same'}))
    before=path.read_bytes()
    with pytest.raises(ValueError,match='Conflicting'):
        upgrade(path)
    assert path.read_bytes()==before


def test_sessions_are_random_and_revocable(settings,auth_memory):
    one=auth.session_token(settings,time.time()+100)
    two=auth.session_token(settings,time.time()+100)
    assert one!=two and len(one)==43
    assert one not in str(auth_memory.rows)
    assert auth.session_user(settings,one)['role']=='admin'
    auth.revoke_session(settings,one)
    assert auth.session_user(settings,one) is None
    assert auth.session_user(settings,two)
    assert auth.revoke_sessions(settings,'admin')==1
    assert auth.session_user(settings,two) is None
    assert auth.session_user(settings,'9999999999.bmFtZQ==.admin.signature') is None


def test_a_second_installation_has_its_own_sign_in_cookie(settings,auth_memory,monkeypatch):
    """Browsers keep cookies by host, not port: two installations on 127.0.0.1 must not share one."""
    named={**settings,'instance':'restore-test'}
    patch_all(monkeypatch,CONFIG_MODULES,'load_config',lambda:named)
    client=TestClient(api.app,base_url='https://testserver')
    assert client.post('/api/login',json={'username':'admin','password':'fixture'}).status_code==200
    token=client.cookies.get('familyfoto_session_restore_test')
    assert token and client.cookies.get('familyfoto_session') is None
    assert client.get('/api/me').status_code==200
    other=TestClient(api.app,base_url='https://testserver')
    # The other installation's cookie name means nothing here.
    other.cookies.set('familyfoto_session',token)
    assert other.get('/api/me').status_code==401


def test_logout_and_admin_revocation(settings,auth_memory,monkeypatch):
    patch_all(monkeypatch,CONFIG_MODULES,'load_config',lambda:settings)
    clients=[TestClient(api.app,base_url='https://testserver') for _ in range(3)]
    admin,guest,second=clients
    try:
        for client,name,password in [(admin,'admin','fixture'),(guest,'guest','guest-password'),
                                     (second,'guest','guest-password')]:
            assert client.post('/api/login',json={'username':name,'password':password}).status_code==200
        cookie=guest.cookies.get('familyfoto_session')
        assert guest.post('/api/auth/revoke',json={'username':'admin'}).status_code==403
        assert guest.get('/api/auth/accounts').status_code==403
        assert guest.post('/api/auth/logout').status_code==200
        assert auth.session_user(settings,cookie) is None
        assert second.get('/api/me').status_code==200
        assert admin.post('/api/auth/revoke',json={'username':'guest'}).json()['revoked']==1
        assert second.get('/api/me').status_code==401
        assert admin.post('/api/auth/logout-all',headers={'origin':'https://evil.test'}).status_code==403
        assert admin.post('/api/auth/logout-all').status_code==200
        assert admin.get('/api/me').status_code==401
    finally:
        for client in clients:client.close()


def test_guest_can_revoke_own_sessions(settings,auth_memory,monkeypatch):
    patch_all(monkeypatch,CONFIG_MODULES,'load_config',lambda:settings)
    client=TestClient(api.app)
    try:
        token=auth.session_token(settings,time.time()+100,'guest')
        client.cookies.set('familyfoto_session',token)
        assert client.post('/api/auth/logout-all').status_code==200
        assert auth.session_user(settings,token) is None
    finally:client.close()


def test_loopback_does_not_bypass_revocation(settings,auth_memory,monkeypatch):
    settings['host']='127.0.0.1'
    settings['browser_accounts']={}
    patch_all(monkeypatch,CONFIG_MODULES,'load_config',lambda:settings)
    client=TestClient(api.app)
    try:
        assert client.get('/api/me').status_code==401
        assert client.post('/api/login',json={'username':'admin','password':'fixture'}).status_code==200
        assert client.post('/api/auth/logout').status_code==200
        assert client.get('/api/me').status_code==401
    finally:client.close()


def test_login_limits_and_invalid_payloads(settings,auth_memory,monkeypatch):
    patch_all(monkeypatch,CONFIG_MODULES,'load_config',lambda:settings)
    client=TestClient(api.app)
    try:
        for payload in ([],None,{'username':[],'password':'x'},{'username':'admin','password':'x'*1025}):
            assert client.post('/api/login',json=payload).status_code==422
        assert client.post('/api/login',content='x'*8193).status_code==413
        auth_memory.retry=17
        with patch.object(auth,'verify_password') as verify:
            response=client.post('/api/login',json={'username':'admin','password':'fixture'})
            assert response.status_code==429 and response.headers['retry-after']=='17'
            verify.assert_not_called()
        auth_memory.retry=0
        with patch.object(auth,'verify_password',wraps=auth.verify_password) as verify:
            assert client.post('/api/login',json={'username':'not-an-account','password':'wrong'}).status_code==401
            assert verify.call_args.args[0]['password_hash']==auth.DUMMY_HASH
    finally:client.close()


def test_login_hash_work_does_not_block_http_loop(settings,auth_memory,monkeypatch):
    patch_all(monkeypatch,CONFIG_MODULES,'load_config',lambda:settings)
    started,release=threading.Event(),threading.Event()
    def slow_login(*args):
        started.set()
        release.wait(3)
        return None,0
    monkeypatch.setattr(auth_api,'authenticate',slow_login)
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app),base_url='http://testserver') as client:
            login=asyncio.create_task(client.post('/api/login',json={'username':'admin','password':'fixture'}))
            try:
                assert await asyncio.to_thread(started.wait,2)
                assert not login.done()
                assert (await asyncio.wait_for(client.get('/login'),1)).status_code==200
            finally:release.set()
            assert (await login).status_code==401
    asyncio.run(exercise())


def test_database_failure_fails_closed(settings,auth_memory,monkeypatch):
    patch_all(monkeypatch,CONFIG_MODULES,'load_config',lambda:settings)
    client=TestClient(api.app)
    def unavailable(*_):raise psycopg.OperationalError('fixture unavailable')
    try:
        client.cookies.set('familyfoto_session','a'*43)
        monkeypatch.setattr(auth_memory,'get',unavailable)
        response=client.get('/api/me')
        assert response.status_code==503 and 'role' not in response.json()
        client.cookies.clear()
        monkeypatch.setattr(auth_memory,'admit_login',unavailable)
        response=client.post('/api/login',json={'username':'admin','password':'fixture'})
        assert response.status_code==503 and 'set-cookie' not in response.headers
    finally:client.close()


@pytest.fixture
def pg_settings(isolated_db,settings):
    _,local,_=isolated_db
    # Resolve the test-only admission callback at call time, not the bound default.
    schema_upgrading.migrate(lambda:database.connection(local), before_upgrade=None)
    return {**settings,'database':local}


def test_postgres_sessions_survive_store_recreation_and_revoke(pg_settings,monkeypatch):
    token=auth.session_token(pg_settings,time.time()+100)
    # Reproduce the People process's recognition-schema override.
    monkeypatch.setattr(database,'load_config',lambda:{'database':{'dbname':'must-not-use'}})
    assert auth.session_user(pg_settings,token)['username']=='admin'
    with database.connection(pg_settings['database']) as db:
        row=db.execute('SELECT * FROM auth_sessions').fetchone()
        assert row['token_hash']==auth.token_digest(token) and token not in str(row)
    SessionStore(pg_settings).revoke_user('admin')
    assert auth.session_user(pg_settings,token) is None
    expired=auth.session_token(pg_settings,1)
    assert auth.session_user(pg_settings,expired) is None


def test_concurrent_login_admission_and_restart(pg_settings):
    def attempt(_):return SessionStore(pg_settings).admit_login('account:one','ip:one')
    with ThreadPoolExecutor(max_workers=8) as executor:
        results=list(executor.map(attempt,range(12)))
    assert results.count(0)==5
    assert all(1<=retry<=60 for retry in results if retry)
    with database.connection(pg_settings['database']) as db:
        before=db.execute("SELECT resets_at FROM auth_login_limits WHERE bucket='account:one'").fetchone()
    assert SessionStore(pg_settings).admit_login('account:one','ip:other')>0
    with database.connection(pg_settings['database']) as db:
        assert db.execute("SELECT resets_at FROM auth_login_limits WHERE bucket='account:one'").fetchone()==before
        db.execute("UPDATE auth_login_limits SET resets_at=clock_timestamp()-interval '1 second'")
    assert SessionStore(pg_settings).admit_login('account:one','ip:one')==0


def test_address_and_global_budgets_bound_unknown_accounts(pg_settings):
    store=SessionStore(pg_settings)
    for i in range(20):assert store.admit_login(f'account:{i}','ip:one')==0
    assert store.admit_login('account:21','ip:one')>0
    for i in range(40):assert store.admit_login(f'account:other{i}',f'ip:{i}')==0
    assert store.admit_login('account:new','ip:new')>0
    with database.connection(pg_settings['database']) as db:
        assert db.execute('SELECT count(*) n FROM auth_login_limits').fetchone()['n']<=121


def test_auth_schema_does_not_wait_for_media_workers():
    migration=next(item for item in schema_upgrading.discover(schema_upgrading.ROOT/'databases') if item.version==25)
    assert migration.guards()==()


def test_activity_preserves_guarantee_and_extends_only_live_sessions(pg_settings,monkeypatch):
    patch_all(monkeypatch,CONFIG_MODULES,'load_config',lambda:pg_settings)
    token=auth.session_token(pg_settings,time.time()+auth.SESSION_SECONDS,'guest')
    digest=auth.token_digest(token)
    client=TestClient(api.app,base_url='https://testserver')
    client.cookies.set('familyfoto_session',token)
    try:
        store=SessionStore(pg_settings)
        assert store.activity(digest)['remaining']>43100
        with database.connection(pg_settings['database']) as db:
            db.execute("UPDATE auth_sessions SET expires_at=CURRENT_TIMESTAMP+interval '2 minutes' WHERE token_hash=%s",(digest,))
        # Polling and normal reads never renew idle expiry.
        assert client.get('/api/me').status_code==200
        with database.connection(pg_settings['database']) as db:
            assert db.execute('SELECT extract(epoch FROM expires_at-CURRENT_TIMESTAMP) remaining FROM auth_sessions WHERE token_hash=%s',(digest,)).fetchone()['remaining']<=120
        assert client.post('/api/auth/activity',headers={'origin':'https://evil.test'}).status_code==403
        response=client.post('/api/auth/activity',headers={'origin':'https://testserver'})
        assert response.status_code==200
        assert 'Max-Age=3600' in response.headers['set-cookie']
        assert 'HttpOnly' in response.headers['set-cookie'] and 'Secure' in response.headers['set-cookie']
        with database.connection(pg_settings['database']) as db:
            db.execute("UPDATE auth_sessions SET expires_at=CURRENT_TIMESTAMP-interval '1 second' WHERE token_hash=%s",(digest,))
        assert store.activity(digest) is None
        assert client.post('/api/auth/activity').status_code==401
        store.revoke(digest)
        assert store.activity(digest) is None
    finally:client.close()
