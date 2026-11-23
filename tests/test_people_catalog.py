import pytest
from test_automatic_people import automatic, pool, env, setup_groups
from app import database
from face_detect.recognition import automatic_people as auto
from face_detect.recognition.people_catalog import ImportCatalog
from test_unexamined_photos import disposable_migrations


def test_start_cannot_name_a_different_import(monkeypatch):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from face_detect.api.automatic_people_api import job_router
    monkeypatch.setattr(auto,'guard',lambda:None)
    app=FastAPI()
    @app.middleware('http')
    async def identity(request,next):
        request.state.authenticated=True
        request.state.user={'username':'fixture','role':'admin'}
        return await next(request)
    def unexpected(*args):raise AssertionError('Must reject before enrollment or dispatch')
    app.include_router(job_router(SimpleNamespace(import_scoped=True),unexpected,unexpected,
                                  raw_media=True,server_selection=True))
    with TestClient(app) as client:
        result=client.post('/api/automatic-people/jobs',json={'id':'import-1','selection':'import:2'})
        assert result.status_code==422
        assert 'selected import' in result.json()['detail']


def test_import_scope_and_existing_decisions_preserved(automatic,env):
    catalog=ImportCatalog(env[1],database.connection)
    with database.connection() as db:
        setup_groups(db,automatic)
        before=auto.catalog(db)
        ids=[r['id'] for r in db.execute('SELECT id FROM media ORDER BY id')]
        jobs=[db.execute("INSERT INTO jobs(source_root,state) VALUES(%s,%s) RETURNING id",(f'fixture:{i}',state)).fetchone()['id'] for i,state in enumerate(['completed-review-required','completed-review-required','running'])]
        db.execute('UPDATE media SET first_import_job_id=%s WHERE id=ANY(%s)',(jobs[0],ids[:2]))
        db.execute('UPDATE media SET first_import_job_id=%s WHERE id=%s',(jobs[1],ids[2]))
        db.execute('UPDATE media SET first_import_job_id=%s WHERE id=%s',(jobs[2],ids[3]))
    assert {r['id'] for r in catalog.selections()}=={f'import:{j}' for j in jobs[:2]}
    assert {r['id']:r['total'] for r in catalog.selections()}=={f'import:{jobs[0]}':2,f'import:{jobs[1]}':1}
    assert catalog.resolve(f'import:{jobs[0]}')==ids[:2]
    assert catalog.resolve(f'import:{jobs[0]}')==ids[:2]
    assert catalog.resolve(f'import:{jobs[1]}')==ids[2:3]
    with pytest.raises(ValueError,match='Finish'):catalog.resolve(f'import:{jobs[2]}')
    for bad in ['year:2020','import:0','import:14;drop','../14']:
        with pytest.raises(ValueError):catalog.resolve(bad)
    with database.connection() as db:
        assert auto.catalog(db)==before
        db.execute("INSERT INTO face_preferences(media_id,opted_out,actor) VALUES(%s,true,'fixture')",(ids[0],))
    assert catalog.resolve(f'import:{jobs[0]}')==ids[1:2]
    assert next(r['total'] for r in catalog.selections() if r['id']==f'import:{jobs[0]}')==1
