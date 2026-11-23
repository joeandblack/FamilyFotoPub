"""Regular accounts upload; each upload is owned and shared or private; levels can be changed."""
import io
import uuid
from PIL import Image
from fastapi.testclient import TestClient
from app import api, database, uploads
from app.auth import password_hash
from test_uploads import env, start, transfer  # noqa: F401


def photo(color):
    stream = io.BytesIO()
    Image.new('RGB', (10, 10), color).save(stream, format='JPEG')
    return stream.getvalue()


def sign_in(name):
    client = TestClient(api.app)
    assert client.post('/api/login', json={'username': name, 'password': 'fixture'}).status_code == 200
    client.headers['X-FamilyFoto-Upload'] = '1'
    return client


def accounts(config):
    config['browser_accounts'].update(other={'role': 'regular', 'password_hash': password_hash('fixture')},
                                      watcher={'role': 'guest', 'password_hash': password_hash('fixture')})


def upload(client, content, name='Trip/a.jpg', **options):
    payload = {'request_key': str(uuid.uuid4()),
               'files': [{'path': name, 'size': len(content), 'modified': 1}], **options}
    response = client.post('/api/uploads', json=payload)
    assert response.status_code == 200, response.text
    session = response.json()
    transfer(client, session, content)
    done = client.post(f"/api/uploads/{session['id']}/finalize")
    assert done.status_code == 200, done.text
    uploads.dispatch_once()
    return session['id'], done.json()['job_id']


def stored():
    with database.connection() as db:
        return db.execute('SELECT id,owner_name,visibility FROM media ORDER BY id').fetchall()


def listed(client):
    return {i['id'] for i in client.get('/api/media').json()['items']}


def test_regular_users_upload_and_choose_who_can_see_it(env):
    admin, config, _ = env
    accounts(config)
    reader, other = sign_in('reader'), sign_in('other')
    upload(reader, photo((10, 20, 30)), 'Trip/shared.jpg')                      # default: everyone
    upload(reader, photo((200, 20, 30)), 'Trip/mine.jpg', visibility='private')
    rows = {r['id']: r for r in stored()}
    assert [(r['owner_name'], r['visibility']) for r in rows.values()] == [('reader', 'shared'), ('reader', 'private')]
    shared, private = list(rows)
    assert listed(reader) == {shared, private} and listed(admin) == {shared, private}
    assert listed(other) == {shared}
    assert other.get(f'/api/media/{private}/thumbnail').status_code == 404
    reader.close(); other.close()


def test_read_only_accounts_cannot_upload_and_batches_are_scoped(env):
    admin, config, _ = env
    accounts(config)
    reader, other, guest = sign_in('reader'), sign_in('other'), sign_in('watcher')
    assert guest.get('/api/uploads').status_code == 403
    assert guest.post('/api/uploads', json={}).status_code == 403
    session, job = upload(reader, photo((1, 2, 3)))
    assert [s['owner'] for s in admin.get('/api/uploads').json()['sessions']] == ['reader']   # administrators see everyone's
    assert [s['id'] for s in reader.get('/api/uploads').json()['sessions']] == [session]
    assert other.get('/api/uploads').json()['sessions'] == []
    assert other.get(f'/api/uploads/{session}').status_code == 409                          # someone else's batch
    assert admin.get(f'/api/uploads/{session}').json()['owner'] == 'reader'
    assert other.post(f'/api/uploads/{session}/cancel').status_code == 409
    assert admin.post(f'/api/uploads/{session}/cancel').status_code == 200                  # administrators may manage it
    for client in (reader, other, guest):
        client.close()


def test_owners_and_administrators_change_levels_nobody_else_can(env):
    admin, config, _ = env
    accounts(config)
    reader, other, guest = sign_in('reader'), sign_in('other'), sign_in('watcher')
    _, job = upload(reader, photo((9, 9, 9)))
    with database.connection() as db:
        imported = db.execute("INSERT INTO media(sha256,relative_path,name,kind,size) VALUES ('abc','x.jpg','x.jpg','image',1) RETURNING id").fetchone()['id']
    mine = stored()[0]['id']
    change = lambda client, **body: client.post('/api/media/visibility', json=body)  # noqa: E731
    assert change(reader, media_ids=[mine], visibility='private').json() == {'updated': 1, 'visibility': 'private'}
    assert listed(other) == {imported}                                           # now hidden from others
    assert change(other, media_ids=[mine], visibility='shared').status_code == 404   # cannot even tell it exists
    assert change(reader, media_ids=[imported], visibility='private').status_code == 403   # shared, not theirs
    assert change(guest, media_ids=[mine], visibility='shared').status_code == 403
    assert change(admin, media_ids=[imported], visibility='private').status_code == 200  # imports can be hidden by an admin
    with database.connection() as db:
        row = db.execute('SELECT owner_name,visibility FROM media WHERE id=%s', (imported,)).fetchone()
    assert (row['owner_name'], row['visibility']) == ('admin', 'private')
    assert listed(reader) == {mine}
    # By batch: all of one upload at once; both targets together are refused.
    assert change(reader, import_job=job, visibility='shared').json()['updated'] == 1
    assert listed(other) == {mine}
    assert change(reader, media_ids=[mine], import_job=job, visibility='shared').status_code == 422
    assert change(other, import_job=job, visibility='private').status_code == 404    # a batch they cannot see
    for client in (reader, other, guest):
        client.close()


def test_uploading_a_private_duplicate_reveals_nothing_and_grants_nothing(env):
    admin, config, _ = env
    accounts(config)
    reader, other = sign_in('reader'), sign_in('other')
    content = photo((50, 60, 70))
    upload(reader, content, 'Trip/secret.jpg', visibility='private')
    _, job = upload(other, content, 'Trip/same.jpg')
    with database.connection() as db:
        item = db.execute('SELECT state,media_id FROM import_items WHERE job_id=%s', (job,)).fetchone()
        assert db.execute('SELECT count(*) n FROM media').fetchone()['n'] == 1
    assert item['state'] == 'duplicate' and item['media_id'] is None          # no link to the hidden item
    assert listed(other) == set() and other.get('/api/media', params={'import_job': job}).json()['items'] == []
    reader.close(); other.close()


def test_upload_status_shows_current_privacy_not_the_original_choice(env):
    admin, config, _ = env
    accounts(config)
    reader = sign_in('reader')
    sid, job = upload(reader, photo((90, 10, 10)), 'Trip/one.jpg', visibility='private')
    current = lambda: reader.get(f'/api/uploads/{sid}').json()  # noqa: E731
    first = current()
    assert (first['requested_visibility'], first['visibility']) == ('private', 'private')
    assert reader.post('/api/media/visibility', json={'import_job': job, 'visibility': 'shared'}).status_code == 200
    after = current()
    assert (after['requested_visibility'], after['visibility'], after['visibility_counts']) == ('private', 'shared', {'shared': 1, 'private': 0})
    assert reader.get('/api/uploads').json()['sessions'][0]['visibility'] == 'shared'
    reader.close()


def test_a_batch_with_items_changed_one_by_one_reports_mixed(env):
    admin, config, _ = env
    accounts(config)
    reader = sign_in('reader')
    content = [photo((20 * i, 30, 40)) for i in range(1, 3)]
    payload = {'request_key': str(uuid.uuid4()), 'files': [{'path': f'Trip/{i}.jpg', 'size': len(c), 'modified': 1} for i, c in enumerate(content)]}
    session = reader.post('/api/uploads', json=payload).json()
    for f, c in zip(session['files'], content):
        single = {**session, 'files': [f]}
        transfer(reader, single, c)
    assert reader.post(f"/api/uploads/{session['id']}/finalize").status_code == 200
    uploads.dispatch_once()
    ids = [r['id'] for r in stored()]
    assert reader.get(f"/api/uploads/{session['id']}").json()['visibility'] == 'shared'
    assert reader.post('/api/media/visibility', json={'media_ids': ids[:1], 'visibility': 'private'}).status_code == 200
    mixed = reader.get(f"/api/uploads/{session['id']}").json()
    assert (mixed['visibility'], mixed['visibility_counts']) == ('mixed', {'shared': 1, 'private': 1})
    reader.close()
