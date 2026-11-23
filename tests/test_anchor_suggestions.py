import pytest
from face_detect.recognition.suggestions import suggest
def row(i,**kw):return dict(id=i,photo_id=i,content_id=i,event_id=i,space_id='m',vector=[1.,0.],seed=True,**kw)
def test_requires_two_events_and_withheld_labels():
    a=[row(1,person_id=7),row(2,person_id=7)];q=[row(3)]
    assert suggest(a,q)[0]['person_id']==7
    a[1]['event_id']=1
    assert suggest(a,q)[0]['reason']=='insufficient_anchor_events'
    with pytest.raises(ValueError):suggest(a,[row(3,person_id=7)])
def test_ambiguity_low_quality_and_outlier_abstain():
    a=[row(i,person_id=7 if i<3 else 8) for i in range(1,5)]
    assert suggest(a,[row(5)])[0]['reason']=='ambiguous'
    q=row(6);q['seed']=False
    assert suggest(a,[q])[0]['reason']=='low_quality'
    q=row(6);q['vector']=[0.,1.]
    assert suggest(a,[q])[0]['reason']=='below_threshold'
