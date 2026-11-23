"""People selection and progress follow the same private-media and upload-owner rules."""
import json
import uuid
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from app import database, viewer
from app.auth import api as auth_api
from face_detect.recognition.people_catalog import ImportCatalog
from test_automatic_people import automatic, pool, env, setup_groups  # noqa: F401
from test_unexamined_photos import disposable_migrations  # noqa: F401


@pytest.fixture
def library(automatic, env):
    """A server import (shared photos) and an upload by 'other' whose photo is private."""
    with database.connection() as db:
        setup_groups(db, automatic)
        ids = [r['id'] for r in db.execute('SELECT id FROM media ORDER BY id')]
        server = db.execute("INSERT INTO jobs(source_root,state) VALUES ('fixture:server','completed-review-required') RETURNING id").fetchone()['id']
        upload = db.execute("INSERT INTO jobs(source_root,source_kind,state) VALUES ('upload:x','upload','completed-review-required') RETURNING id").fetchone()['id']
        db.execute("INSERT INTO upload_sessions(id,owner_name,request_key,manifest_hash,label,options,total_bytes,job_id) VALUES (%s,'other',%s,'h','Trip','{}',1,%s)",
                   (uuid.uuid4(), uuid.uuid4(), upload))
        db.execute('UPDATE media SET first_import_job_id=%s WHERE id=ANY(%s)', (server, ids[:2]))
        db.execute("UPDATE media SET first_import_job_id=%s,owner_name='other',visibility='private' WHERE id=%s", (upload, ids[2]))
        db.execute("UPDATE media SET first_import_job_id=%s,owner_name='other' WHERE id=%s", (upload, ids[3]))
    return ImportCatalog(env[1], database.connection), ids, server, upload, env[1]


def test_selections_resolve_and_visible_jobs_follow_the_viewer(library):
    catalog, ids, server, upload, _ = library
    assert {r['id']: r['total'] for r in catalog.selections()} == {f'import:{server}': 2, f'import:{upload}': 2}
    assert catalog.visible_job_ids() >= {server, upload}
    with viewer.acting_as('reader'):
        # Neither the other person's upload batch nor its private photo is discoverable.
        assert {r['id'] for r in catalog.selections()} == {f'import:{server}'}
        assert upload not in catalog.visible_job_ids() and server in catalog.visible_job_ids()
        with pytest.raises(ValueError, match='Finish'):
            catalog.resolve(f'import:{upload}')
        assert catalog.resolve(f'import:{server}') == ids[:2]
    with viewer.acting_as('other'):
        assert {r['id']: r['total'] for r in catalog.selections()} == {f'import:{server}': 2, f'import:{upload}': 2}
        assert catalog.resolve(f'import:{upload}') == ids[2:4]


def test_the_cache_is_per_viewer(library):
    catalog, _, server, upload, _ = library
    assert f'import:{upload}' in {r['id'] for r in catalog.selections(cache_seconds=600)}
    with viewer.acting_as('reader'):
        assert {r['id'] for r in catalog.selections(cache_seconds=600)} == {f'import:{server}'}
    assert f'import:{upload}' in {r['id'] for r in catalog.selections(cache_seconds=600)}


def test_people_application_routes_hide_other_peoples_imports(library, monkeypatch, tmp_path):
    from face_detect.api.application import application
    from face_detect.api.import_status import import_statuses
    from app.auth.deps import reader as reader_dependency
    from fastapi import Depends
    catalog, _, server, upload, config = library
    monkeypatch.setattr(auth_api, 'load_config', lambda: config)
    monkeypatch.setattr(auth_api, 'session_user', lambda cfg, cookie: {'username': cookie, 'role': 'admin' if cookie == 'admin' else 'regular'} if cookie else None)
    for number in (server, upload):
        folder = tmp_path / 'jobs' / f'import-{number}'
        folder.mkdir(parents=True)
        (folder / 'import.json').write_text(json.dumps({'id': number}))
    jobs = SimpleNamespace(status=lambda name, user: {'state': 'running', 'done': 1, 'total': 2, 'excluded': 0, 'errors': [f'file-of-{name}.jpg']})
    app = application(tmp_path, jobs=jobs, dispatch=lambda *a: True, protect=auth_api.protect, catalog=catalog)

    @app.get('/api/automatic-people/import-status')
    def status(user=Depends(reader_dependency)):
        return import_statuses(catalog, jobs, tmp_path, user)

    with TestClient(app) as client:
        def ask(who, path):
            client.cookies.set('familyfoto_session', who)
            return client.get(path)
        for who, seen in (('admin', {server, upload}), ('other', {server, upload}), ('reader', {server})):
            listed = {int(r['id'].split(':')[1]) for r in ask(who, '/api/automatic-people/selections').json()}
            progress = {int(k) for k in ask(who, '/api/automatic-people/import-status').json()}
            assert listed == seen and progress == seen, who
        # The direct job route, including child jobs, applies the same rule.
        hidden_names = (f'import-{upload}', f'import-{upload}-photo-5', 'import-99999')
        for name in hidden_names:
            gone = ask('reader', f'/api/automatic-people/jobs/{name}')
            assert gone.status_code == 404 and 'file-of' not in gone.text, name
            assert gone.json() == ask('reader', '/api/automatic-people/jobs/import-99998').json()
        for who in ('admin', 'other'):
            owned = ask(who, f'/api/automatic-people/jobs/import-{upload}')
            assert owned.status_code == 200 and f'file-of-import-{upload}.jpg' in owned.text, who
            assert ask(who, f'/api/automatic-people/jobs/import-{upload}-photo-5').status_code == 200, who
        shared = ask('reader', f'/api/automatic-people/jobs/import-{server}')
        assert shared.status_code == 200 and f'file-of-import-{server}.jpg' in shared.text
        assert f'file-of-import-{upload}.jpg' in ask('admin', '/api/automatic-people/import-status').text
        assert f'file-of-import-{upload}.jpg' not in ask('reader', '/api/automatic-people/import-status').text
