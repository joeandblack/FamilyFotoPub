from fastapi.testclient import TestClient
from app import api, database
from test_uploads import env, image_bytes, start, transfer  # noqa: F401


def sign_in(name):
    client = TestClient(api.app)
    assert client.post('/api/login', json={'username': name, 'password': 'fixture'}).status_code == 200
    return client


def make_upload(client):
    content = image_bytes()
    session, _ = start(client, ['Holiday/a.jpg'], content)
    transfer(client, session, content)
    response = client.post(f"/api/uploads/{session['id']}/finalize")
    assert response.status_code == 200, response.text
    return response.json()['job_id']


def test_upload_batches_are_private_to_their_uploader_and_administrators(env):
    admin, _, _ = env
    upload_job = make_upload(admin)
    with database.connection() as db:
        server_job = db.execute("INSERT INTO jobs(source_root,source_label) VALUES ('server-folder','Server copy') RETURNING id").fetchone()['id']
    reader = sign_in('reader')

    ids = lambda client, query='': {j['id']: j for j in client.get('/api/import/history' + query).json()['imports']}  # noqa: E731
    assert ids(admin)[upload_job]['uploader'] == 'admin'
    assert ids(admin, '?summary=true')[upload_job]['uploader'] == 'admin'
    for query in ('', '?summary=true'):
        seen = ids(reader, query)
        assert upload_job not in seen and server_job in seen, (query, list(seen))
    assert upload_job not in ids(reader, f'?job_id={upload_job}')

    for method, path in (('get', f'/api/import/{upload_job}/diagnostics'),
                         ('get', f'/api/import/{upload_job}/photo-duplicates'),
                         ('post', f'/api/import/{upload_job}/pause')):
        assert getattr(reader, method)(path).status_code == 404, path
    assert admin.get(f'/api/import/{upload_job}/diagnostics').status_code == 200
    assert reader.get(f'/api/import/{server_job}/diagnostics').status_code == 200
    reader.close()


def test_issue_list_hides_other_peoples_upload_filenames(env):
    admin, _, _ = env
    upload_job = make_upload(admin)
    with database.connection() as db:
        db.execute("UPDATE import_items SET state='unsupported',error='secret-name' WHERE job_id=%s", (upload_job,))
    reader = sign_in('reader')
    assert 'Holiday/a.jpg' not in reader.get('/api/issues').text
    assert 'Holiday/a.jpg' in admin.get('/api/issues').text
    reader.close()
