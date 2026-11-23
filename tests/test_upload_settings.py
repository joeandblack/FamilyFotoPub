import hashlib
from fastapi.testclient import TestClient
from app import api, uploads
from test_uploads import env, image_bytes, start, transfer  # noqa: F401

GIB = 1024**3


def valid(**changes):
    return {**{k: uploads.DEFAULTS[k] for k in uploads.EDITABLE}, **changes}


def test_defaults_are_the_original_hard_coded_numbers():
    assert uploads.DEFAULTS == dict(max_files=5000, max_total_bytes=20*GIB, max_file_bytes=5*GIB,
                                    staging_bytes=40*GIB, chunk_bytes=8*1024**2, reserve_bytes=5*GIB,
                                    expiry_hours=48, max_active=1, upload_mibps=12)


def test_admin_changes_limits_and_restores_defaults(env):
    client, config, _ = env
    view = client.get('/api/upload-settings').json()
    assert view['defaults']['max_files'] == 5000 and view['values']['chunk_bytes'] == 64  # from config
    response = client.put('/api/upload-settings', json=valid(max_files=7))
    assert response.status_code == 200, response.text
    assert uploads.limits()['max_files'] == 7
    content = image_bytes()
    too_many = {'request_key': '6d0c1a58-6f43-4a0b-a60d-7c9a4f3a0001',
                'files': [{'path': f'Trip/{i}.jpg', 'size': len(content), 'modified': 1} for i in range(8)]}
    assert client.post('/api/uploads', json=too_many).status_code == 409
    assert client.delete('/api/upload-settings').status_code == 200
    assert uploads.limits()['max_files'] == 5000
    start(client, ['Trip/one.jpg'], content)


def test_invalid_settings_are_refused_and_not_stored(env):
    client, _, _ = env
    for bad in (valid(max_files=0), valid(max_files='9'), valid(chunk_bytes=32*1024**2),
                valid(max_file_bytes=30*GIB), valid(upload_mibps=100000),
                {**valid(), 'max_active': 2}, {k: v for k, v in valid().items() if k != 'max_files'}):
        response = client.put('/api/upload-settings', json=bad)
        assert response.status_code == 409, (bad, response.text)
    with uploads.connection() as db:
        assert db.execute("SELECT count(*) n FROM app_settings").fetchone()['n'] == 0


def test_only_administrators_can_read_or_change_limits(env):
    client, _, _ = env
    other = TestClient(api.app)
    other.post('/api/login', json={'username': 'reader', 'password': 'fixture'})
    other.headers['X-FamilyFoto-Upload'] = '1'
    assert other.get('/api/upload-settings').status_code == 403
    assert other.put('/api/upload-settings', json=valid()).status_code == 403
    assert other.delete('/api/upload-settings').status_code == 403
    other.close()


def test_started_upload_keeps_the_limits_it_was_accepted_with(env):
    client, config, _ = env
    content = image_bytes()
    session, _ = start(client, ['Trip/one.jpg'], content)
    assert session['limits']['chunk_bytes'] == 64 and session['limits']['expiry_hours'] == 48
    # Another administrator changes Settings after this upload started.
    assert client.put('/api/upload-settings', json=valid(chunk_bytes=1024**2, expiry_hours=1)).status_code == 200
    assert uploads.limits()['chunk_bytes'] == 1024**2
    # The started session still enforces (and reports) its own chunk size and expiry.
    refreshed = client.get(f"/api/uploads/{session['id']}").json()
    assert refreshed['limits']['chunk_bytes'] == 64 and refreshed['limits']['expiry_hours'] == 48
    too_big = client.put(f"/api/uploads/{session['id']}/files/0?offset=0", content=content[:100],
                         headers={'X-Content-SHA256': hashlib.sha256(content[:100]).hexdigest()})
    assert too_big.status_code == 409
    transfer(client, session, content)  # completes with the original 64-byte pieces
    with uploads.connection() as db:
        db.execute("UPDATE upload_sessions SET updated_at=now()-interval '3 hours' WHERE id=%s", (session['id'],))
    uploads.dispatch_once()  # the new 1-hour expiry must not cancel a 48-hour session
    assert client.get(f"/api/uploads/{session['id']}").json()['state'] == 'uploading'
    assert client.post(f"/api/uploads/{session['id']}/finalize").status_code == 200
    # An upload started after the change gets the new limits.
    later, _ = start(client, ['Trip/two.jpg'], content, key='6d0c1a58-6f43-4a0b-a60d-7c9a4f3a0002')
    assert later['limits']['expiry_hours'] == 1
