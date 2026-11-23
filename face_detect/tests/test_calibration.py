import pytest
from face_detect.recognition.calibration import calibrate,native_seed
from face_detect.recognition.native_organize import plan_model


def test_date_split_and_heldout_predictions():
    rows=[dict(id=d*3+p,person_id=p,event_id=f'2026-01-{d+1:02}',photo_id=d*3+p,content_id=str(d*3+p),space_id='test',vector=[float(i==p) for i in range(3)],seed=True) for d in range(9) for p in range(3)]
    report=calibrate(rows)
    assert report['validation_dates']==3 and report['validation']['queries']==9
    assert report['validation']['wrong']==0
    assert not report['qualified'] # Small sample cannot pass the activation gate.
    with pytest.raises(ValueError):calibrate(rows[:6])


def test_native_quality_does_not_rewrite_receipts():
    face={'box':dict(x_min=0,y_min=0,x_max=100,y_max=100,probability=.88),'landmarks':[[20,20],[80,20],[50,40],[30,70],[70,70]]}
    assert native_seed(face)
    assert face['box']['probability']==.88
    assert not native_seed(face,.9)


def test_native_grouping_obeys_separations_and_manual_memberships():
    rows=[dict(id=i,photo=i,content=str(i),event_id=str(i),vector=[1.,0.],group=i,seed=True,query=True,trusted=False,person_id=None) for i in (1,2)]
    data={'native_policies':{'m':{'threshold':.55,'margin':.2}},'blocks':set(),'constraints':{frozenset((1,2))},'members':{i:{'group_id':i,'origin':'automatic'} for i in (1,2)}}
    assert plan_model(data,'m',rows)=={}
    data['constraints']=set()
    assert plan_model(data,'m',rows)=={2:{'group_id':1,'space_id':'m'}}
    data['members'][2]['origin']='manual'
    assert plan_model(data,'m',rows)=={}


def test_calibration_predictions_match_production_suggestions():
    from face_detect.recognition.calibration import validation_predictions
    from face_detect.recognition.suggestions import suggest
    import random
    import math
    rng=random.Random(12)
    records=[]
    for i in range(40):
        vector=[rng.uniform(-1,1) for _ in range(8)]
        norm=math.sqrt(sum(v*v for v in vector))
        records.append(dict(id=i,person_id=i%3,space_id='m',event_id=str(i//3),photo_id=i,
                            content_id=str(i),seed=i%7!=0,vector=[v/norm for v in vector]))
    anchors=records[:30]
    queries=[{k:v for k,v in r.items() if k!='person_id'} for r in records[30:]]
    expected=suggest(anchors,queries,0,0)
    actual=validation_predictions(anchors,queries)
    for a,b in zip(actual,expected):
        assert a['person_id']==b['person_id']
        assert a['score']==pytest.approx(b['score'])
        assert a['margin']==pytest.approx(b['margin'])


def test_large_assessment_keeps_all_queries():
    from face_detect.recognition.calibration import assess,tuning_sample
    rows=[dict(id=i,person_id=i%3,space_id='m',event_id=str(i),photo_id=i,
               content_id=str(i),seed=False,vector=[1.,0.]) for i in range(201)]
    assert assess([],rows,.7,.2)['queries']==201
    sample=tuning_sample(rows)
    assert len(sample)==150 and len({r['person_id'] for r in sample})==3
    assert tuning_sample(list(reversed(rows)))==sample


def test_failed_calibration_cannot_activate():
    from face_detect.cli.calibrate import activate
    with pytest.raises(ValueError,match='did not qualify'):
        activate({'qualified':False})


@pytest.mark.parametrize('value',[None,'','unknown-date','unknown-da','None','2026-02-30'])
def test_missing_or_invalid_dates_have_one_complete_sentinel(value):
    from face_detect.recognition.calibration import capture_event_id
    assert capture_event_id(value)=='unknown-date'


def test_capture_date_and_timestamp_are_the_same_event():
    from datetime import datetime,date
    from face_detect.recognition.calibration import capture_event_id
    for value in ('2026-01-02','2026-01-02T23:30:00+08:00',datetime(2026,1,2,23,30),date(2026,1,2)):
        assert capture_event_id(value)=='2026-01-02'


def test_old_truncated_missing_date_cannot_enter_holdout():
    rows=[dict(id=d*3+p,person_id=p,event_id=f'2026-01-{d+1:02}',photo_id=d*3+p,
               content_id=str(d*3+p),space_id='m',vector=[float(i==p) for i in range(3)],seed=True)
          for d in range(9) for p in range(3)]
    unknown=[{**rows[0],'id':100+i,'photo_id':100+i,'content_id':str(100+i),'event_id':value}
             for i,value in enumerate((None,'unknown-date','unknown-da',''))]
    report=calibrate(rows+unknown)
    assert report['unknown_date_faces_excluded']==4
    assert report['available_training_labels']==18
    assert report['validation']['queries']==9
    assert unknown[2]['event_id']=='unknown-da'  # Inputs and persisted labels stay untouched.


def test_equivalent_age_groups_are_scored_as_one_identity_without_relabeling():
    from face_detect.recognition.calibration import assess,identity_map
    rows=[dict(id=i,person_id=10 if i<3 else 12,event_id=f'2026-01-{i:02}',photo_id=i,
               content_id=str(i),space_id='m',vector=[1.,0.],seed=True) for i in (1,2,3)]
    assert assess(rows[:2],rows[2:],.65,.2)['wrong']==1
    corrected=assess(rows[:2],rows[2:],.65,.2,identities=identity_map([[10,12]]))
    assert corrected['wrong']==0 and corrected['same_identity_cross_group_matches']==1
    assert rows[2]['person_id']==12
    assert assess([],rows[1:],.65,.2)['mixed_groups']==1
    groups=assess([],rows[1:],.65,.2,identities=identity_map([[10,12]]))
    assert groups['mixed_groups']==0 and groups['same_identity_cross_group_clusters']==1


def test_missing_identity_test_withholds_all_age_groups(monkeypatch):
    from face_detect.recognition import calibration as module
    original=module.assess
    calls=[]
    def record(anchors,queries,*args,**kwargs):
        calls.append(({r['person_id'] for r in anchors},{r['person_id'] for r in queries}))
        return original(anchors,queries,*args,**kwargs)
    monkeypatch.setattr(module,'assess',record)
    rows=[dict(id=d*3+i,person_id=pid,event_id=f'2026-01-{d+1:02}',photo_id=d*3+i,
               content_id=str(d*3+i),space_id='m',vector=[1.,0.] if pid in (10,12) else [0.,1.],seed=True)
          for d in range(9) for i,pid in enumerate((10,12,20))]
    report=module.calibrate(rows,[[10,12]])
    assert report['people']==2 and report['person_groups']==3
    assert calls[-2:]==[({20},{10,12}),({10,12},{20})]
    assert report['validation']['unknown_identity_false_matches']==0


@pytest.mark.parametrize('groups',[[[10]],[[10,10]],[[10,12],[12,20]],[[True,12]]])
def test_equivalences_reject_ambiguous_configuration(groups):
    from face_detect.recognition.calibration import identity_map
    with pytest.raises(ValueError):identity_map(groups)


def test_equivalences_persist_locally_without_catalog_edits(tmp_path,monkeypatch):
    from face_detect.cli import calibrate as cli
    from contextlib import contextmanager
    import json
    pilot='a'*32
    root=tmp_path/'face_detect/profiles'/pilot
    root.mkdir(parents=True)
    (tmp_path/'people-preview.json').write_text(json.dumps({'pilot':pilot}))
    (root/'runtime.json').write_text(json.dumps({'space_id':'m','schema':'face_native_'+pilot}))
    class ReadOnly:
        def execute(self,sql,args=()):
            assert sql=='SET TRANSACTION READ ONLY' or sql.startswith('SELECT id FROM people')
            if args:self.ids=args[0]
            return self
        def fetchall(self):return [{'id':pid} for pid in self.ids]
    @contextmanager
    def connection(*args):yield ReadOnly()
    monkeypatch.setattr(cli,'DATA',tmp_path)
    monkeypatch.setattr(cli,'load_config',lambda:{'database':{}})
    monkeypatch.setattr(cli.database,'connection',connection)
    cli.save_equivalences([[10,12]])
    assert cli.read_equivalences(root)==[[10,12]]
    cli.save_equivalences([[12,10]])
    assert cli.read_equivalences(root)==[[10,12]]
    cli.save_equivalences([[10,12,27]])
    assert cli.read_equivalences(root)==[[10,12,27]]
    with pytest.raises(ValueError,match='non-overlapping'):
        cli.save_equivalences([[27,30]])
    assert cli.read_equivalences(root)==[[10,12,27]]
