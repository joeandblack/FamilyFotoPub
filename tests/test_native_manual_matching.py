from face_detect.recognition import policy
from face_detect.recognition.people_relaxed import propose
from face_detect.recognition.native_organize import plan_model


def test_trial_does_not_enable_discovery(monkeypatch):
    saved = {'calibrated': False, 'manual_trial': {'enabled': True, 'threshold': .65, 'margin': .2}}
    monkeypatch.setattr(policy, 'matching_policy', lambda db, model: saved)
    assert policy.manual_enabled(policy.manual_policy(None, 'native'))
    assert not policy.matching_policy(None, 'native')['calibrated']
    assert 'manual_enabled' not in saved


def test_historical_model_stays_disabled(monkeypatch):
    monkeypatch.setattr(policy, 'matching_policy', lambda db, model: {'calibrated': False, 'mode': 'historical-only'})
    assert not policy.manual_enabled(policy.manual_policy(None, 'old'))


def test_trial_off_stays_disabled(monkeypatch):
    monkeypatch.setattr(policy, 'matching_policy', lambda db, model: {'calibrated': False, 'manual_trial': {'enabled': False}})
    assert not policy.manual_enabled(policy.manual_policy(None, 'native'))


def test_native_suggestions_use_native_threshold():
    anchors = [dict(id=i, person_id=1, space_id='native', vector=[1., 0.],
                    event_id=str(i), photo_id=i, content_id=str(i), seed=True) for i in (1, 2)]
    queries = [dict(id=3, space_id='native', vector=[.4, .916515139],
                    event_id='3', photo_id=3, content_id='3', seed=True)]
    assert propose(anchors, queries)
    assert not propose(anchors, queries, threshold=.5)
    queries[0]['vector'] = [.8, .6]
    assert propose(anchors, queries, threshold=.5)[0]['person_id'] == 1
    assert not propose(anchors[:1], queries, threshold=.5)


def test_trial_auto_organize_preserves_manual_memberships_and_constraints():
    rows = [dict(id=i, photo=i, content=str(i), event_id=str(i), vector=[1., 0.],
                 group=i, seed=True, query=True, trusted=False, person_id=None) for i in (1, 2)]
    data = {'native_policies': {'native': {'threshold': .65, 'margin': .2}},
            'blocks': set(), 'constraints': set(),
            'members': {i: {'group_id': i, 'origin': 'automatic'} for i in (1, 2)}}
    assert plan_model(data, 'native', rows) == {2: {'group_id': 1, 'space_id': 'native'}}
    data['members'][2]['origin'] = 'manual'
    assert plan_model(data, 'native', rows) == {}
    data['members'][2]['origin'] = 'automatic'
    data['constraints'] = {frozenset((1, 2))}
    assert plan_model(data, 'native', rows) == {}
