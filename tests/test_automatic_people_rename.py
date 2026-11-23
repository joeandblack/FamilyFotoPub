import pytest
from test_automatic_people import automatic,pool,env,setup_groups
from app import database
from face_detect.recognition import automatic_people as auto


def test_rename_preserves_identity_membership_and_rejects_collisions(automatic):
    with database.connection() as db:
        ga,gb,pa,pb=setup_groups(db,automatic)
        before=db.execute('SELECT * FROM auto_people_members ORDER BY detection_id').fetchall()
        auto.rename_person(db,pa,' New name ','fixture')
        assert next(g for g in auto.catalog(db)['groups'] if g['id']==ga)['name']=='New name'
        assert db.execute('SELECT * FROM auto_people_members ORDER BY detection_id').fetchall()==before
        assert auto.rename_person(db,pa,'New name','fixture')['unchanged']
        with pytest.raises(RuntimeError):auto.rename_person(db,pa,'fixture b','fixture')
        for name in ('   ','bad\nname','x'*101):
            with pytest.raises(ValueError):auto.rename_person(db,pa,name,'fixture')
        with pytest.raises(LookupError):auto.rename_person(db,999999,'Missing','fixture')


def test_rename_api_role_and_stale_revision(automatic):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from face_detect.api.automatic_people_api import router
    with database.connection() as db:
        _,_,pid,_=setup_groups(db,automatic);revision=auto.catalog(db)['revision']
    app=FastAPI()
    @app.middleware('http')
    async def identity(request,next):
        request.state.authenticated=True
        request.state.user={'username':'fixture','role':request.headers.get('x-role','admin')}
        return await next(request)
    app.include_router(router)
    with TestClient(app) as client:
        path=f'/api/automatic-people/people/{pid}/rename';body={'revision':revision,'name':'Changed'}
        assert client.post(path,json=body,headers={'x-role':'regular'}).status_code==403
        assert client.post(path,json=body|{'revision':revision-1}).status_code==409
        assert client.post(path,json=body).status_code==200
        assert client.post(path,json=body).status_code==409
        assert any(g['person_id']==pid and g['name']=='Changed' for g in client.get('/api/automatic-people').json()['groups'])
