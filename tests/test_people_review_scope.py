from contextlib import nullcontext
import pytest
from fastapi import HTTPException
from test_automatic_people import automatic, pool, env, observations
from face_detect.recognition import automatic_people as auto
from app import database
from face_detect.recognition import people_auto_organize as organize
from face_detect.api.automatic_people_api import invoke


def setup(db, ids):
    auto.initialize_review_sections(db, [])
    auto.ingest(db, 'scope-test', 'synthetic-auto', observations(ids))
    a = db.execute("INSERT INTO jobs(source_root,state) VALUES('scope-a','completed') RETURNING id").fetchone()['id']
    b = db.execute("INSERT INTO jobs(source_root,state) VALUES('scope-b','completed') RETURNING id").fetchone()['id']
    for index, did in enumerate(ids):
        db.execute('UPDATE media SET first_import_job_id=%s WHERE id=(SELECT a.media_id FROM '
                   'face_analyses a JOIN face_detections d ON d.analysis_id=a.id WHERE d.id=%s)',
                   (a if index % 2 == 0 else b, did))
    return a, b


def test_catalog_detail_and_references_are_scoped_independently(automatic):
    with database.connection() as db:
        a, b = setup(db, automatic)
        view = auto.catalog(db, import_id=a)
        assert {f['detection_id'] for g in view['groups'] for f in g['faces']} == set(automatic[::2])
        gid = view['groups'][0]['id']
        assert len(auto.detail(db, gid, import_id=a)['group']['faces']) == 1
        pid = auto.name_group(db, gid, 'Reference', 'test', import_id=b)['person_id']
        snap = organize.snapshot(db, a)
        rows = [r for cohort in snap['models'].values() for r in cohort]
        assert any(r['trusted'] and r['person_id'] == pid for r in rows)
        assert {r['id'] for r in rows if r['query']} == set(automatic[::2])
        assert auto.catalog(db, import_id=999999)['groups'] == []


def test_review_pages_preserve_import_scope(automatic):
    from face_detect.api.people_review_pages import review_page
    with database.connection() as db:
        a,b=setup(db,automatic)
        for scope in [a,b,999999]:
            expected=auto.catalog(db,import_id=scope)['groups']
            for section in ['review','other']:
                result=review_page(db,section,1,100,scope)
                wanted=[g for g in expected if g['section']==section]
                assert result['groups']==wanted
                assert result['total']==len(wanted)


@pytest.mark.parametrize('action', ['ignore_review_groups', 'set_aside_groups'])
def test_bulk_actions_only_change_filtered_faces(automatic, action):
    with database.connection() as db:
        a, b = setup(db, automatic)
        view = auto.catalog(db, import_id=a)
        group = view['groups'][0]
        did = group['faces'][0]['detection_id']
        hidden_before = auto.catalog(db, import_id=b)['groups']
    invoke(action, [group['id']], 'test', revision=view['revision'], import_id=a,
           group_ids=[group['id']], group_versions={str(group['id']): group['review_version']},
           group_faces={str(group['id']): [did]}, reconcile=True)
    with database.connection() as db:
        assert auto.catalog(db, import_id=b)['groups'] == hidden_before


def test_filtered_name_splits_hidden_group_members(automatic):
    with database.connection() as db:
        a, b = setup(db, automatic)
        group = auto.catalog(db, import_id=a)['groups'][0]
        pid = auto.name_group(db, group['id'], 'Scoped name', 'test', import_id=a)['person_id']
        assert auto.detail(db, group['id'], import_id=b)['group']['person_id'] is None
        named = next(g for g in auto.catalog(db)['groups'] if g['person_id'] == pid)
        assert named['faces'] == [dict(group['faces'][0], origin='named_seed')]


def test_forged_out_of_scope_face_is_rejected(automatic):
    with database.connection() as db:
        a, b = setup(db, automatic)
        revision = auto.catalog(db)['revision']
    with pytest.raises(HTTPException) as error:
        invoke('ignore', automatic[1], True, 'test', revision=revision, face_id=automatic[1], import_id=a)
    assert error.value.status_code == 422


def test_scoped_organize_splits_targets_and_undo_restores(automatic):
    with database.connection() as db:
        a, b = setup(db, automatic)
        data = organize.snapshot(db, a)
        before = {r['detection_id']: dict(r) for r in db.execute('SELECT * FROM auto_people_members').fetchall()}
        target = before[automatic[0]]['group_id']
        proposal = {'policy': organize.POLICY, 'moves': {automatic[2]: {'group_id': target, 'space_id': 'synthetic-auto'}},
                    'summary': {'matched_faces': 0, 'groups_before': 2, 'groups_after': 1}}
        result = organize.apply(db, data, proposal, 'test')
        current = {r['detection_id']: dict(r) for r in db.execute('SELECT * FROM auto_people_members').fetchall()}
        assert current[automatic[1]] == before[automatic[1]]
        assert current[automatic[0]]['group_id'] == current[automatic[2]]['group_id'] != target
        assert organize.undo(db, result['event_id'], 'test')['restored'] == 2
        assert {r['detection_id']: dict(r) for r in db.execute('SELECT * FROM auto_people_members').fetchall()} == before


def test_recheck_accepts_detected_portion_without_completed_scan(automatic, tmp_path, monkeypatch):
    from face_detect.jobs.automatic_people_import import ImportJobs
    from face_detect.jobs.automatic_people_jobs import Jobs
    from face_detect.jobs import automatic_people_jobs
    from face_detect.recognition import people_relaxed
    calls = []
    monkeypatch.setattr(automatic_people_jobs, 'manual_lease', lambda progress: nullcontext())
    monkeypatch.setattr(auto, 'recheck_unnamed', lambda db, ids, actor: calls.append(ids) or {'matched': 0})
    monkeypatch.setattr(people_relaxed, 'generate', lambda *args, **kwargs: {'suggestions': 0})
    with database.connection() as db:
        a, b = setup(db, automatic)
        from face_detect.recognition.people_review_scope import media_ids
        expected = media_ids(db, a)
    jobs = ImportJobs(Jobs(tmp_path, None, None, None))
    jobs.recheck('completed-imports', 0, {'username': 'test', 'role': 'admin'}, import_id=a)
    assert calls == [sorted(expected)]


def test_suggestions_from_different_imports_remain_available(automatic):
    from test_people_relaxed import seed
    from test_automatic_people import add_face
    from face_detect.recognition import people_relaxed as relaxed
    import math
    with database.connection() as db:
        ids, pid, mid = seed(db)
        a = db.execute("INSERT INTO jobs(source_root,state) VALUES('suggest-a','completed') RETURNING id").fetchone()['id']
        b = db.execute("INSERT INTO jobs(source_root,state) VALUES('suggest-b','completed') RETURNING id").fetchone()['id']
        did2, mid2 = add_face(db, 950)
        auto.ingest(db, 'second-suggestion', 'synthetic-auto', [{'detection_id': did2, 'seed': True, 'vector': [.35, math.sqrt(1-.35**2)]}])
        db.execute('UPDATE media SET first_import_job_id=%s WHERE id=%s', (a, mid))
        db.execute('UPDATE media SET first_import_job_id=%s WHERE id=%s', (b, mid2))
        first = relaxed.generate(db, [mid], 'test', import_id=a)
        relaxed.generate(db, [mid2], 'test', import_id=b)
        assert relaxed.collection(db)['total'] == 2
        assert [c['detection_id'] for c in relaxed.collection(db, import_id=a)['items']] == [ids[-1]]
        relaxed.decide(db, first['suggestion_run'], ids[-1], pid, 'reject', 'test')
        assert relaxed.collection(db, import_id=b)['total'] == 1


def test_http_filter_and_bulk_match_contract(automatic):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from face_detect.api.automatic_people_api import router
    from face_detect.api.automatic_people_api import reader
    from face_detect.api.automatic_people_api import administrator
    import json
    with database.connection() as db:
        a, b = setup(db, automatic)
        view = auto.catalog(db, import_id=a)
        target_group = view['groups'][1]['id']
        pid = auto.name_group(db, target_group, 'Target outside import', 'test', import_id=b)['person_id']
    app = FastAPI();app.include_router(router)
    app.dependency_overrides[reader] = lambda: {'username': 'test', 'role': 'admin'}
    app.dependency_overrides[administrator] = app.dependency_overrides[reader]
    with TestClient(app) as client:
        prefix = '/api/automatic-people'
        view = client.get(prefix, params={'import_id': a}).json()
        group = view['groups'][0]
        ids = [f['detection_id'] for f in group['faces']]
        response = client.post(prefix+'/review-actions/stream', json={
            'revision': view['revision'], 'import_id': a, 'action': 'match', 'section': 'review',
            'person_id': pid, 'group_ids': [group['id']],
            'group_faces': {str(group['id']): ids}, 'group_versions': {str(group['id']): group['review_version']}})
        result = json.loads(response.text.splitlines()[-1])
        assert result['state'] == 'completed' and result['result']['matched'] == 1
        detail = client.get(prefix+f"/groups/{group['id']}", params={'import_id': b}).json()
        assert detail['group']['person_id'] is None


def test_filter_choices_include_only_scanned_imports(automatic):
    from face_detect.recognition.people_review_scope import scanned_imports
    with database.connection() as db:
        a,b=setup(db,automatic)
        empty=db.execute("INSERT INTO jobs(source_root,state) VALUES('not-scanned','completed') RETURNING id").fetchone()['id']
        choices=scanned_imports(db)
        assert {r['id'] for r in choices}=={f'import:{a}',f'import:{b}'}
        assert all(r['total']>0 and 'scanned photos' in r['label'] for r in choices)
        assert f'import:{empty}' not in {r['id'] for r in choices}


def test_filter_does_not_load_unrelated_face_metadata(automatic,monkeypatch):
    with database.connection() as db:
        a,b=setup(db,automatic)
        original=auto.rows;loaded=[]
        def scoped_rows(db,ids=None):
            assert ids is not None
            loaded.extend(ids)
            return original(db,ids)
        monkeypatch.setattr(auto,'rows',scoped_rows)
        auto.catalog(db,import_id=a)
        assert set(loaded)==set(automatic[::2])
