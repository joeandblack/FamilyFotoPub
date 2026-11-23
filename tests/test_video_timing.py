from datetime import datetime, timezone
from app.video.progress import timing


def test_current_run_only_and_paused_clock():
    class DB:
        def execute(self,query,args):
            assert "v.updated_at >= %s" in query
            assert "v.state IN ('done','warning','ancillary')" in query
            self.args=args
            return self
        def fetchone(self): return {'count':10}
    db=DB()
    video={'state':'running','started':'2026-10-12T00:00:00+00:00','updated':'2026-10-12T01:00:00+00:00'}
    now=datetime(2026,10,12,2,tzinfo=timezone.utc)
    result=timing(video,db,now)
    assert result['run_seconds']==7200 and result['run_processed']==10
    video['state']='paused'
    assert timing(video,db,now)['run_seconds']==3600
    assert 'run_seconds' not in video


def test_missing_legacy_dates_are_safe():
    assert timing(None,None) is None
    assert timing({'started':'bad'},None)=={'started':'bad'}


def test_current_file_uses_catalog_without_reading_media():
    from unittest.mock import Mock
    from app.video.progress import current_details
    db=Mock()
    db.execute.return_value.fetchone.return_value={'name':'clip.mov','size':1234,'metadata':None}
    video={'state':'running','current':42}
    assert current_details(video,db)['current_file']=={'name':'clip.mov','size':1234,'duration_seconds':None}
    assert 'current_file' not in video
    video['current_duration_seconds']=12.5
    assert current_details(video,db)['current_file']['duration_seconds']==12.5
    db.reset_mock()
    video['state']='paused'
    assert current_details(video,db)['current_file'] is None
    db.execute.assert_not_called()


def test_current_file_missing_or_previously_probed():
    from unittest.mock import Mock
    from app.video.progress import current_details
    db=Mock()
    db.execute.return_value.fetchone.return_value=None
    assert current_details({'state':'running','current':1},db)['current_file'] is None
    db.execute.return_value.fetchone.return_value={'name':'a.mp4','size':0,'metadata':{'duration_seconds':0}}
    assert current_details({'state':'running','current':1},db)['current_file']['duration_seconds']==0


def test_conversion_runtime_and_new_completions_only(monkeypatch):
    from app.video import progress
    monkeypatch.setattr(progress,'_conversion_samples',{})
    first=datetime(2026,10,13,2,tzinfo=timezone.utc)
    status={'state':'running','started':'2026-10-13T00:00:00+00:00','conversion_counts':{'done':444,'warning':1}}
    initial=progress.conversion_timing(status,first)
    assert initial['run_seconds']==7200 and initial['run_processed']==0
    status['conversion_counts']['done']=447
    later=progress.conversion_timing(status,datetime(2026,10,13,2,10,tzinfo=timezone.utc))
    assert later['estimate_seconds']==600 and later['run_processed']==3
    status.update(state='paused',updated='2026-10-13T02:10:00+00:00')
    assert progress.conversion_timing(status,datetime(2026,10,14,tzinfo=timezone.utc))['run_seconds']==7800
    assert progress._conversion_samples == {}


def test_paused_import_history_does_not_reset_active_conversion_estimate(monkeypatch):
    from app.video import progress
    monkeypatch.setattr(progress,'_conversion_samples',{})
    first=datetime(2026,10,13,2,tzinfo=timezone.utc)
    active={'job_id':8,'state':'running','started':first.isoformat(),'conversion_counts':{'done':10}}
    progress.conversion_timing(active,first)
    other={**active,'job_id':2,'state':'paused','updated':first.isoformat()}
    progress.conversion_timing(other,first)
    active['conversion_counts']={'done':12}
    later=progress.conversion_timing(active,datetime(2026,10,13,2,10,tzinfo=timezone.utc))
    assert later['run_processed']==2 and later['estimate_seconds']==600
    # Resuming a group establishes a new throughput sample, preserving saved time.
    active.update(started='2026-10-13T03:00:00+00:00',elapsed_base=600)
    resumed=progress.conversion_timing(active,datetime(2026,10,13,3,tzinfo=timezone.utc))
    assert resumed['run_processed']==0 and resumed['run_seconds']==600


def test_conversion_file_keeps_input_size_after_publication():
    from unittest.mock import Mock
    from app.video.progress import current_details
    db=Mock();db.execute.return_value.fetchone.return_value={'name':'clip.avi','size':200,'original_size':1000,'metadata':{}}
    result=current_details({'state':'running','phase':'avi-conversion','current':1},db)
    assert result['current_file']['size']==1000
