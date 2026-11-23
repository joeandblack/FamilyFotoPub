import time
import pytest
from app.service.readonly_preview import MemorySessions,allowed_request


@pytest.fixture
def sessions():
    MemorySessions.rows={}
    MemorySessions.limits={}
    return MemorySessions({})


def test_memory_sessions_are_shared_revocable_and_expiring(sessions):
    sessions.create('one','admin','signature',time.time()+100)
    other=MemorySessions({})
    assert other.get('one')['username']=='admin'
    other.revoke('one')
    assert sessions.get('one') is None
    sessions.create('expired','admin','signature',time.time()-1)
    assert sessions.get('expired') is None
    assert sessions.activity('expired') is None


def test_preview_login_throttles_without_database(sessions):
    for _ in range(5):assert sessions.admit_login('account','address')==0
    assert sessions.admit_login('account','address')>0


def test_preview_activity_and_logout_all(sessions):
    sessions.create('one','admin','signature',time.time()+100)
    sessions.create('two','admin','signature',time.time()+100)
    assert sessions.activity('one')['remaining']>=3599
    assert sessions.revoke_user('admin')==2
    assert sessions.get('two') is None


@pytest.mark.parametrize('method,path',[
    ('POST','/api/media/1/trash'),('POST','/api/face-service/start'),
    ('DELETE','/api/albums/1'),('PATCH','/api/album-collections/1'),
    ('POST','/api/import/start'),('GET','/api/downloads/prepare'),
    ('GET','/api/uploads'),('GET','/api/people-preview'),
])
def test_preview_rejects_mutations_and_side_effecting_reads(method,path):
    assert not allowed_request(method,path)


@pytest.mark.parametrize('method,path',[
    ('GET','/api/albums'),('GET','/api/media/1/original'),
    ('HEAD','/web/app.js'),('POST','/api/login'),('POST','/api/auth/activity'),
])
def test_preview_permits_reads_and_memory_auth(method,path):
    assert allowed_request(method,path)


def test_people_readonly_is_explicit_and_never_permits_changes():
    assert allowed_request('GET','/api/people-preview/cards',True)
    assert not allowed_request('POST','/api/people-preview/groups/1/name',True)
    assert not allowed_request('POST','/api/people-preview/jobs',True)
    assert not allowed_request('DELETE','/api/people-preview/groups/1',True)


def test_people_catalog_identity_validation(tmp_path):
    import json
    from face_detect.api.readonly_people import catalog_settings
    pilot='a'*32
    (tmp_path/'people-preview.json').write_text(json.dumps({'enabled':True,'pilot':pilot}))
    root=tmp_path/'compreface/raw-local'/pilot
    root.mkdir(parents=True)
    (root/'runtime.json').write_text(json.dumps({'schema':'auto_eval_'+pilot}))
    assert catalog_settings(tmp_path)['root']==root
    (root/'runtime.json').write_text(json.dumps({'schema':'public'}))
    with pytest.raises(ValueError):catalog_settings(tmp_path)


def test_a_recovered_library_with_a_fresh_native_catalog_passes_the_startup_check(tmp_path):
    """A catalog from a fresh install, or one a Mac adopted, has no preserved_catalog flag; it
    opens. A catalog from before the one catalog doesn't, until it's migrated."""
    import json
    from app.service.readonly_preview import people_schema
    pilot='a'*32
    root=tmp_path/'face_detect/profiles'/pilot
    root.mkdir(parents=True)
    (tmp_path/'people-preview.json').write_text(json.dumps({'enabled':True,'backend':'native','pilot':pilot}))
    (root/'runtime.json').write_text(json.dumps({'schema':'people'}))
    preview={'data_root':str(tmp_path),'people_readonly':True}
    assert people_schema({'preview':preview,'recovery':{'activation_required':True}})=='people'
    # The one catalog is readable with or without a recovery, and needs no 'preserved' mark.
    assert people_schema({'preview':preview})=='people'
    (root/'runtime.json').write_text(json.dumps({'schema':'face_native_'+pilot}))
    with pytest.raises(ValueError):people_schema({'preview':preview,'recovery':{'activation_required':True}})


def test_blurry_thumbnails_do_not_require_group_membership():
    from face_detect.api.readonly_people import thumbnail_query
    blurry = thumbnail_query('blurry')
    assert 'auto_people_members' not in blurry
    assert "q.state='blurry'" in blurry
    assert 'content_hash=m.sha256' in blurry
    assert 'auto_people_members' in thumbnail_query('group')
    assert 'd.ignored' in thumbnail_query('ignored')
    with pytest.raises(ValueError):thumbnail_query('unknown')


def test_windows_extended_paths_preserve_write_containment(tmp_path):
    import sys
    from app.service.readonly_preview import resolved_write_path
    if sys.platform != 'win32':pytest.skip('Windows path namespace')
    allowed=tmp_path/'private'
    allowed.mkdir()
    ordinary=allowed/'file.txt'
    assert resolved_write_path('\\\\?\\'+str(ordinary)) == ordinary.resolve()
    escaped='\\\\?\\'+str(allowed/'..'/'outside.txt')
    assert not resolved_write_path(escaped).is_relative_to(allowed.resolve())
