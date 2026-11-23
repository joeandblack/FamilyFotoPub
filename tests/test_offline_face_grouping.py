import importlib.util
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('offline',Path(__file__).parents[1]/'face_detect/recognition/grouping.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def item(i,v=(1.,0.),**kw):
    return dict(id=i,vector=list(v),space_id='test',photo_id=i,content_id=i,event_id=i,seed=True,**kw)

def test_complete_link_prevents_bridge_and_keeps_outlier():
    import math
    rows=[item(i,(math.cos(t),math.sin(t))) for i,t in enumerate((0,.5,1.,3.))]
    groups=m.cluster(rows,.8)
    assert sorted(len(g['ids']) for g in groups)==[1,1,2]

def test_same_photo_duplicates_and_constraints_block_merge():
    for field in ('photo_id','content_id'):
        a,b=item(1),item(2);b[field]=a[field]
        assert len(m.cluster([a,b],.8))==2
    assert len(m.cluster([item(1),item(2)],.8,[(1,2)]))==2

def test_burst_is_not_independent_support():
    rows=[item(1),item(2)];rows[1]['event_id']=1
    assert not m.cluster(rows,.8)[0]['supported']

def test_low_quality_cannot_seed_but_can_attach():
    rows=[item(1),item(2),item(3)];rows[2]['seed']=False
    assert len(m.cluster(rows,.8))==1
    rows[1]['seed']=False
    assert len(m.cluster(rows,.8))==3

def test_normalization_and_model_guards():
    assert m.unit([3,4],2)==([.6,.8],5)
    for vector in ([0,0],[float('nan'),1],[1]):
        with pytest.raises(ValueError):m.unit(vector,2)
    rows=[item(1),item(2)];rows[1]['space_id']='other'
    with pytest.raises(ValueError):m.cluster(rows,.8)
    with pytest.raises(ValueError):m.cluster([item(1,(2,0))],.8)

def test_deterministic_and_labels_unused():
    rows=[item(1),item(2)];rows[0]['person_id']=1;rows[1]['person_id']=2
    assert m.cluster(rows,.8)==m.cluster(list(reversed(rows)),.8)
    assert len(m.cluster(rows,.8))==1

def test_ambiguous_low_quality_remains_isolated():
    import math
    rows=[item(i,(math.cos(t),math.sin(t))) for i,t in enumerate((-.3,-.3,.3,.3,0))]
    rows[-1]['seed']=False
    groups=m.cluster(rows,.9)
    assert sorted(len(g['ids']) for g in groups)==[1,2,2]

def test_trusted_anchor_conflicts_cannot_merge_through_unlabeled_bridge():
    rows=[item(1),item(2),item(3)]
    groups=m.cluster(rows,.8,anchors={1:'known-a',3:'known-b'})
    assert all(not {1,3}.issubset(g['ids']) for g in groups)
    assert len(groups)==2
    with pytest.raises(ValueError):m.cluster(rows,.8,anchors={99:'unknown'})

def test_metric_denominators_and_no_calibration_without_negatives():
    spec=importlib.util.spec_from_file_location('metrics',Path(__file__).parents[1]/'scripts/evaluate_offline_grouping.py')
    metrics=importlib.util.module_from_spec(spec);spec.loader.exec_module(metrics)
    labels={1:'a',2:'a',3:'b',4:'b'}
    result=metrics.grouping_report([{'ids':[1,2,3],'supported':True},{'ids':[4],'supported':False}],labels)
    assert (result['purity_numerator'],result['purity_denominator'])==(2,3)
    assert (result['correct_pairs'],result['false_pairs'],result['same_person_pairs'])==(1,2,2)
    assert metrics.choose_threshold([(.9,True)]) is None
