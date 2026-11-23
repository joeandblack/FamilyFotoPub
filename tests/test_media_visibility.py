"""Private media: only its uploader and administrators may see it, on every surface."""
import hashlib
import io
import json
import uuid
from pathlib import Path
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from app import api, database, viewer
from app.library import downloads
from app.auth import password_hash
from test_uploads import env  # noqa: F401


def jpeg(color):
    stream = io.BytesIO()
    Image.new('RGB', (12, 12), color).save(stream, format='JPEG')
    return stream.getvalue()


@pytest.fixture
def library(env, monkeypatch):
    """A shared photo, reader's private + shared photos, and other's private photo."""
    admin, config, data = env
    config['browser_accounts'].update(other={'role': 'regular', 'password_hash': password_hash('fixture')},
                                      watcher={'role': 'guest', 'password_hash': password_hash('fixture')})
    config['destination'].update(drive='fixture', volume_serial='fixture')
    monkeypatch.setattr(downloads, 'load_config', lambda: config)
    monkeypatch.setattr(downloads, 'volume_serial', lambda _: 'fixture')
    root = Path(config['destination']['root'])
    rows = {}
    with database.connection() as db:
        job = db.execute("INSERT INTO jobs(source_root,state) VALUES ('server-folder','completed-review-required') RETURNING id").fetchone()['id']
        for index, (label, owner, visibility, year) in enumerate([
                ('shared', None, 'shared', 2020), ('mine', 'reader', 'private', 2021),
                ('theirs', 'other', 'private', 2001), ('shared_by_reader', 'reader', 'shared', 2022)]):
            raw = jpeg((index * 40, 90, 160))
            sha = hashlib.sha256(raw).hexdigest()
            relative = f'{label}.jpg'
            (root / relative).write_bytes(raw)
            thumbnails = data / 'thumbnails'
            thumbnails.mkdir(exist_ok=True)
            (thumbnails / f'{sha}.jpg').write_bytes(raw)
            rows[label] = db.execute(
                "INSERT INTO media(sha256,relative_path,name,kind,size,owner_name,visibility,captured_at) VALUES (%s,%s,%s,'image',%s,%s,%s,%s) RETURNING id",
                (sha, relative, f'{label}.jpg', len(raw), owner, visibility, f'{year}-06-01')).fetchone()['id']
            db.execute("INSERT INTO import_items(job_id,source_path,size,mtime_ns,state,media_id) VALUES (%s,%s,1,0,'imported',%s)", (job, f'in/{label}.jpg', rows[label]))
    return admin, rows, job, config


def sign_in(name):
    client = TestClient(api.app)
    assert client.post('/api/login', json={'username': name, 'password': 'fixture'}).status_code == 200
    return client


def listed(client, **params):
    return {i['id'] for i in client.get('/api/media', params=params).json()['items']}


def test_database_rule_and_connection_binding(library):
    _, rows, _, _ = library
    def visible_ids():
        with database.connection() as db:
            return {r['id'] for r in db.execute('SELECT id FROM media m WHERE ' + viewer.media_visible('m')).fetchall()}
    everything = set(rows.values())
    assert visible_ids() == everything  # no viewer: workers, scripts, administrators
    with viewer.acting_as('reader'):
        assert visible_ids() == everything - {rows['theirs']}
        with database.connection() as db:
            # A rolled-back statement must not drop the viewer binding.
            with pytest.raises(Exception):
                db.execute('SELECT 1/0')
            db.rollback()
            assert {r['id'] for r in db.execute('SELECT id FROM media m WHERE ' + viewer.media_visible('m')).fetchall()} == everything - {rows['theirs']}
    assert visible_ids() == everything  # the pooled connection was cleaned on return
    with viewer.acting_as(viewer.NOBODY):
        assert visible_ids() == {rows['shared'], rows['shared_by_reader']}


def test_library_lists_only_what_each_person_may_see(library):
    admin, rows, _, _ = library
    reader, other, guest = sign_in('reader'), sign_in('other'), sign_in('watcher')
    every = set(rows.values())
    assert listed(admin) == every
    assert listed(reader) == every - {rows['theirs']}
    assert listed(other) == every - {rows['mine']}
    assert listed(guest) == {rows['shared'], rows['shared_by_reader']}
    assert reader.get('/api/media').json()['total'] == 3
    assert reader.get('/api/media').json()['stats']['total'] == 3
    # The year picker and the search must not reveal hidden photos either.
    assert 2001 in admin.get('/api/media').json()['years'] and 2001 not in reader.get('/api/media').json()['years']
    assert listed(reader, search='theirs') == set() and listed(other, search='theirs') == {rows['theirs']}
    for client in (reader, other, guest):
        client.close()


def test_files_and_edits_of_hidden_media_are_not_found(library):
    admin, rows, _, _ = library
    reader, other = sign_in('reader'), sign_in('other')
    assert reader.get(f"/api/media/{rows['mine']}/thumbnail").status_code == 200
    assert admin.get(f"/api/media/{rows['mine']}/thumbnail").status_code == 200
    assert other.get(f"/api/media/{rows['mine']}/thumbnail").status_code == 404
    for client in (reader, other):
        client.headers['X-FamilyFoto-Upload'] = '1'
    hidden = rows['theirs']
    assert reader.post(f'/api/media/{hidden}/rotate').status_code == 404
    assert reader.post(f'/api/media/{hidden}/crop', json={'x': 0, 'y': 0, 'width': 1, 'height': 1}).status_code in (404, 422)
    assert reader.post(f'/api/media/{hidden}/trash/trash').status_code == 404
    with database.connection() as db:
        assert db.execute('SELECT count(*) n FROM media_adjustments').fetchone()['n'] == 0
        assert db.execute('SELECT count(*) n FROM media_trash').fetchone()['n'] == 0
    assert other.post(f'/api/media/{hidden}/rotate').status_code == 200
    reader.close(); other.close()


@pytest.mark.parametrize('kind', ['image', 'video'])
def test_recently_deleted_is_personal_and_direct_access_is_guarded(library, kind):
    admin, rows, _, _ = library
    reader, other, guest = sign_in('reader'), sign_in('other'), sign_in('watcher')
    for client in (reader, other):
        client.headers['X-FamilyFoto-Upload'] = '1'
    mine, theirs = rows['shared'], rows['shared_by_reader']
    with database.connection() as db:
        db.execute('UPDATE media SET kind=%s WHERE id=ANY(%s)', (kind, [mine, theirs]))
    assert reader.post(f'/api/media/{mine}/trash/trash').status_code == 200
    assert other.post(f'/api/media/{theirs}/trash/trash').status_code == 200
    # Unattributed historical/system trash is not assigned to an arbitrary user.
    with database.connection() as db:
        db.execute('INSERT INTO media_trash(media_id) VALUES (%s)', (rows['mine'],))
    assert listed(reader, kind='trash') == {mine}
    assert listed(other, kind='trash') == {theirs}
    assert listed(guest, kind='trash') == set()
    assert listed(admin, kind='trash') == {mine, theirs, rows['mine']}
    result = reader.get('/api/media', params={'kind': 'trash', 'page_size': 1}).json()
    assert result['total'] == result['stats']['total'] == 1
    assert result['pages'] == 1
    for hidden in (theirs, rows['mine']):
        assert reader.get(f'/api/media/{hidden}/thumbnail').status_code == 404
        assert reader.get(f'/api/media/{hidden}/original').status_code == 404
        assert reader.post(f'/api/media/{hidden}/trash/restore').status_code == 404
        assert reader.post(f'/api/media/{hidden}/trash/trash').status_code == 404
    assert reader.get(f'/api/media/{mine}/thumbnail').status_code == 200
    assert reader.post(f'/api/media/{mine}/trash/delete').status_code == 403
    # Even administrators repeating trash cannot steal the original attribution.
    assert admin.post(f'/api/media/{mine}/trash/trash').status_code == 200
    assert reader.post(f'/api/media/{mine}/trash/trash').status_code == 200
    with database.connection() as db:
        assert db.execute('SELECT deleted_by FROM media_trash WHERE media_id=%s', (mine,)).fetchone()['deleted_by'] == 'reader'
    assert reader.post(f'/api/media/{mine}/trash/restore').status_code == 200
    assert other.post(f'/api/media/{mine}/trash/trash').status_code == 200
    assert listed(reader, kind='trash') == set()
    assert listed(other, kind='trash') == {mine, theirs}
    assert admin.post(f'/api/media/{mine}/trash/restore').status_code == 200
    assert admin.post(f'/api/media/{mine}/trash/trash').status_code == 200
    with database.connection() as db:
        assert db.execute('SELECT deleted_by FROM media_trash WHERE media_id=%s', (mine,)).fetchone()['deleted_by'] == 'admin'
    assert listed(other, kind='trash') == {theirs}
    assert admin.post(f'/api/media/{rows["mine"]}/trash/restore').status_code == 200
    for client in (reader, other, guest):
        client.close()


def test_trash_actor_does_not_override_current_media_visibility(library):
    admin, rows, _, _ = library
    reader = sign_in('reader')
    reader.headers['X-FamilyFoto-Upload'] = '1'
    media_id = rows['shared']
    assert reader.post(f'/api/media/{media_id}/trash/trash').status_code == 200
    with database.connection() as db:
        db.execute("UPDATE media SET owner_name='other',visibility='private' WHERE id=%s", (media_id,))
    assert listed(reader, kind='trash') == set()
    assert reader.post(f'/api/media/{media_id}/trash/restore').status_code == 404
    assert listed(admin, kind='trash') == {media_id}
    reader.close()


def test_albums_and_collections_show_each_viewer_only_their_photos(library):
    admin, rows, job, _ = library
    album = admin.post('/api/albums', json={'name': 'Everything', 'media_ids': list(rows.values())}).json()['id']
    reader, other = sign_in('reader'), sign_in('other')
    assert {i['id'] for i in reader.get('/api/media', params={'album': album}).json()['items']} == set(rows.values()) - {rows['theirs']}
    summary = {a['id']: a for a in reader.get('/api/albums').json()['albums']}
    assert summary[album]['item_count'] == 3
    assert {a['id']: a for a in other.get('/api/albums').json()['albums']}[album]['item_count'] == 3
    assert admin.get(f'/api/albums/{album}').json()['item_count'] == 4
    # Cannot put someone else's private photo into an album, directly or via an import.
    reader.headers['X-FamilyFoto-Upload'] = '1'
    mine = reader.post('/api/albums', json={'name': 'Mine'}).json()['id']
    assert reader.post(f'/api/albums/{mine}/items', json={'media_ids': [rows['theirs']]}).status_code == 404
    from_import = reader.post('/api/albums', json={'name': 'From import', 'import_job': job}).json()['id']
    assert {i['id'] for i in reader.get('/api/media', params={'album': from_import}).json()['items']} == set(rows.values()) - {rows['theirs']}
    # A collection cover chosen by an administrator must not expose a hidden photo.
    collection = admin.post('/api/album-collections', json={'name': 'Trips'}).json()['id']
    admin.post(f'/api/album-collections/{collection}/albums', json={'album_ids': [album]})
    with database.connection() as db:
        db.execute('UPDATE album_collections SET cover_media_id=%s WHERE id=%s', (rows['theirs'], collection))
    assert admin.get(f'/api/album-collections/{collection}').json()['cover_id'] == rows['theirs']
    assert reader.get(f'/api/album-collections/{collection}').json()['cover_id'] != rows['theirs']
    reader.close(); other.close()


def test_zip_download_refuses_hidden_items(library):
    admin, rows, _, _ = library
    wanted = [rows['shared'], rows['theirs']]
    with viewer.acting_as('reader'):
        with pytest.raises(Exception) as refused:
            downloads.Archive(wanted)
    assert getattr(refused.value, 'status_code', None) == 404
    archive = downloads.Archive(wanted)  # administrators and system code see everything
    archive.close()


def test_duplicate_groups_never_reveal_a_private_copy(library):
    from app.photos import duplicates as photo_duplicates
    _, rows, job, _ = library
    with database.connection() as db:
        for label in ('shared', 'theirs'):
            sha = db.execute('SELECT sha256 FROM media WHERE id=%s', (rows[label],)).fetchone()['sha256']
            db.execute("INSERT INTO photo_fingerprints(media_id,version,stored_sha256,pixel_hash,image_hash,evidence) VALUES (%s,%s,%s,'px','im','{}')",
                       (rows[label], photo_duplicates.VERSION, sha))
        assert len(photo_duplicates.groups(db, job)['groups']) == 1
    with viewer.acting_as('reader'), database.connection() as db:
        assert photo_duplicates.groups(db, job)['groups'] == []
    with viewer.acting_as('other'), database.connection() as db:
        assert len(photo_duplicates.groups(db, job)['groups']) == 1


def test_progress_snapshot_hides_other_peoples_upload_filenames(library):
    admin, rows, _, config = library
    from test_upload_access import make_upload
    job = make_upload(admin)
    from app import system_api
    data = system_api.DATA
    (data / 'import-status.json').write_text(json.dumps({'id': job, 'state': 'running', 'current': 'Holiday/a.jpg',
                                                          'source_root': 'upload:secret', 'started_at': '2026-01-01T00:00:00+00:00'}))
    reader = sign_in('reader')
    assert admin.get('/api/status').json()['job']['current'] == 'Holiday/a.jpg'
    assert reader.get('/api/status').json()['job']['current'] is None
    reader.close()


def test_people_photo_lookups_skip_hidden_media(library):
    from face_detect.api.readonly_people import live_photos
    _, rows, _, config = library
    expected = {}
    with database.connection() as db:
        for row in db.execute('SELECT id,sha256 FROM media').fetchall():
            expected[row['id']] = row['sha256']
    cfg = {'database': config['database']}
    assert {p['id'] for p in live_photos(cfg, expected)} == set(rows.values())
    with viewer.acting_as('reader'):
        assert {p['id'] for p in live_photos(cfg, expected)} == set(rows.values()) - {rows['theirs']}
    with viewer.acting_as(viewer.NOBODY):
        assert rows['mine'] not in {p['id'] for p in live_photos(cfg, expected)}


def test_every_request_path_that_reads_media_applies_the_rule():
    """Guard rail: update this list when a new request-path module reads the media table."""
    root = Path(__file__).resolve().parents[1]
    for name in ('app/library/api.py', 'app/library/downloads.py', 'app/library/trash.py', 'app/albums/__init__.py',
                 'app/albums/collections.py', 'app/photos/duplicates.py', 'app/photos/duplicate_trash.py',
                 'app/video/progress.py', 'app/video/conversion_review.py', 'face_detect/recognition/people_catalog.py', 'face_detect/api/readonly_people.py',
                 'face_detect/jobs/automatic_people_local.py'):
        text = (root / name).read_text(encoding='utf-8')
        assert 'media_visible' in text or 'familyfoto_can_see' in text, name


def test_album_membership_probe_removal_and_move_ignore_hidden_items(library):
    admin, rows, _, _ = library
    album = admin.post('/api/albums', json={'name': 'Everything', 'media_ids': list(rows.values())}).json()['id']
    reader = sign_in('reader')
    reader.headers['X-FamilyFoto-Upload'] = '1'
    hidden = rows['theirs']
    def members():
        with database.connection() as db:
            return {r['media_id'] for r in db.execute('SELECT media_id FROM album_items WHERE album_id=%s', (album,)).fetchall()}
    # Probing a hidden id reveals nothing, and a visible id still works.
    assert reader.post('/api/albums/membership', json={'media_ids': [hidden]}).json() == {'albums': []}
    assert [a['id'] for a in reader.post('/api/albums/membership', json={'media_ids': [rows['mine']]}).json()['albums']] == [album]
    assert [a['id'] for a in admin.post('/api/albums/membership', json={'media_ids': [hidden]}).json()['albums']] == [album]
    # Removal and moves cannot touch a hidden item; the answer matches "not in this album".
    assert reader.post(f'/api/albums/{album}/remove', json={'media_ids': [hidden]}).json() == {'removed': 0}
    target = reader.post('/api/albums', json={'name': 'Target'}).json()['id']
    moved = reader.post(f'/api/albums/{album}/transfer', json={'media_ids': [hidden], 'target_id': target})
    assert moved.status_code == 409
    assert hidden in members()
    assert reader.post(f'/api/albums/{album}/remove', json={'media_ids': [rows['mine']]}).json() == {'removed': 1}
    assert members() == set(rows.values()) - {rows['mine']}
    reader.close()
