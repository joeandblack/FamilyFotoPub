import json
import os
from contextlib import contextmanager
from unittest.mock import Mock,patch
import pytest
from app import jobs as pipeline
from app.video import metadata as videos
from app.video import conversions
from app.jobs.job_scope import membership,require_import_complete,elapsed_base


@pytest.mark.parametrize('stage,complete,paused,options,expected',[
    ('import',True,False,{'auto_metadata':True,'auto_conversion':True},'metadata'),
    ('metadata',True,False,{'auto_metadata':False,'auto_conversion':True},'conversion'),
    ('conversion',True,False,{'auto_metadata':True,'auto_conversion':True},None),
    ('import',True,False,{'auto_metadata':False,'auto_conversion':True},'metadata'),
    ('import',True,False,{'auto_duplicates':True},'duplicates'),
    ('metadata',True,False,{'auto_duplicates':True},'duplicates'),
    ('conversion',True,False,{'auto_duplicates':True},'duplicates'),
    ('duplicates',True,False,{'auto_duplicates':True},None),
    ('conversion',True,True,{'auto_duplicates':True},None),
    ('conversion',False,False,{'auto_duplicates':True},None),
    ('metadata',False,False,{'auto_metadata':True,'auto_conversion':True},None),
    ('metadata',True,True,{'auto_metadata':True,'auto_conversion':True},None),
])
def test_transitions(stage,complete,paused,options,expected):
    assert pipeline.next_stage(stage,complete,options,paused)==expected


def test_scope_is_parameterized_membership():
    assert membership()=='m.first_import_job_id=%s'
    assert 'owned.first_import_job_id=%s' in membership('v')
    assert 'owned.id=v.media_id' in membership('v')
    with pytest.raises(ValueError):membership('untrusted')


def test_only_selected_import_prerequisites():
    db=Mock();db.execute.return_value.fetchone.side_effect=[{'state':'completed-review-required'},{'count':0}]
    require_import_complete(db,4)
    assert all(call.args[1]==(4,) for call in db.execute.call_args_list)


def test_unscoped_workers_refuse():
    for worker in (videos.run,conversions.run):
        with pytest.raises(RuntimeError,match='import group'):worker()


def test_reservation_refuses_busy_worker_before_io(tmp_path):
    db=Mock();db.execute.return_value.fetchone.side_effect=[{'available':True},{'available':False}]
    with patch.object(pipeline,'DATA',tmp_path):
        with pytest.raises(RuntimeError,match='active'):pipeline.idle(db)


def test_startup_retries_transient_reservation_without_holding_partial_locks():
    db=Mock()
    with patch.object(pipeline,'idle',side_effect=[pipeline.ReservationBusy('busy'),None]) as idle,patch.object(pipeline.time,'sleep'):
        pipeline.acquire_startup(db)
    assert idle.call_count==2
    db.execute.assert_called_once_with('SELECT pg_advisory_unlock_all()')
    db.commit.assert_called_once()


def test_startup_wait_is_bounded():
    db=Mock()
    with patch.object(pipeline,'idle',side_effect=pipeline.ReservationBusy('busy')):
        with pytest.raises(pipeline.ReservationBusy):pipeline.acquire_startup(db,timeout=0)
    db.execute.assert_called_once_with('SELECT pg_advisory_unlock_all()')


def test_launch_reserves_priority_before_subprocess_even_without_people(tmp_path,monkeypatch):
    from app import config
    from app.jobs import work_admission
    from app.jobs import job_scope
    db=Mock();db.execute.return_value.fetchone.return_value={
        'state':'completed-review-required','source_kind':'server','auto_metadata':False,
        'auto_conversion':False,'auto_duplicates':False}
    @contextmanager
    def connection():yield db
    monkeypatch.setattr(pipeline,'connection',connection)
    monkeypatch.setattr(pipeline,'idle',lambda db:None)
    monkeypatch.setattr(pipeline,'DATA',tmp_path)
    monkeypatch.setattr(pipeline,'ROOT',tmp_path)
    (tmp_path/'logs').mkdir()
    monkeypatch.setattr(work_admission,'PATH',tmp_path/'handoff.json')
    monkeypatch.setattr(work_admission,'face_owns_reservation',lambda db:False)
    monkeypatch.setattr(job_scope,'require_import_complete',lambda *a:None)
    monkeypatch.setattr(job_scope,'pending_work',lambda *a:True)
    monkeypatch.setattr(conversions,'check_ready',lambda *a:None)
    monkeypatch.setattr(conversions,'save_parameters',lambda *a:None)
    monkeypatch.setattr(config,'load_config',lambda:{'avi_conversion':{'enabled':True}})
    launched=[]
    def launch(command,**kwargs):
        request=work_admission.read()
        assert work_admission.pending() and request['job_id']==4
        assert command[-2:]==['--handoff',request['token']]
        launched.append(command)
    monkeypatch.setattr(pipeline.subprocess,'Popen',launch)
    pipeline.launch(4,'conversion')
    assert len(launched)==1


@pytest.mark.skipif(os.name != 'nt', reason='Windows priority API contract')
@patch('app.video.metadata.ctypes.get_last_error',return_value=402)
@patch('app.video.metadata.ctypes.WinDLL')
def test_sequence_never_changes_group(kernel_factory,last_error,tmp_path):
    kernel_factory.return_value.SetPriorityClass.side_effect=[True,False]
    db=Mock();db.execute.return_value.fetchone.return_value={'source_root':'selected','state':'completed-review-required','auto_metadata':True,'auto_conversion':True}
    @contextmanager
    def connection():yield db
    calls=[]
    def metadata(**kw):
        videos.background_priority()
        calls.append(('metadata',kw['job_id']))
        (tmp_path/'video-status.json').write_text(json.dumps({'state':'completed-review-required'}))
    def conversion(**kw):
        conversions.background_priority()
        calls.append(('conversion',kw['job_id']))
        (tmp_path/'avi-status.json').write_text(json.dumps({'state':'completed-review-required'}))
    with patch.object(pipeline,'DATA',tmp_path),patch.object(pipeline,'connection',connection),patch.object(pipeline,'idle'),patch('app.jobs.job_scope.require_import_complete'),patch('app.jobs.job_scope.pending_work',return_value=True),patch('app.imports.importer.run') as importer,patch.object(videos,'run',metadata),patch.object(conversions,'run',conversion):
        pipeline.run(4,'import')
        importer.assert_called_once_with('selected')
    assert calls==[('metadata',4),('conversion',4)]
    assert kernel_factory.return_value.SetPriorityClass.call_count==2
    assert json.loads((tmp_path/'pipeline-status.json').read_text())['state']=='completed'


def test_pause_stops_handoff(tmp_path):
    db=Mock();db.execute.return_value.fetchone.return_value={'auto_metadata':True,'auto_conversion':True}
    @contextmanager
    def connection():yield db
    def metadata(**kw):
        (tmp_path/'PIPELINE-PAUSE').touch()
        (tmp_path/'video-status.json').write_text(json.dumps({'state':'completed-review-required'}))
    with patch.object(pipeline,'DATA',tmp_path),patch.object(pipeline,'connection',connection),patch.object(pipeline,'idle'),patch('app.jobs.job_scope.require_import_complete'),patch('app.jobs.job_scope.pending_work',return_value=True),patch.object(videos,'run',metadata),patch.object(conversions,'run') as conversion:
        pipeline.run(2,'metadata')
        conversion.assert_not_called()


def test_elapsed_excludes_pause_interval():
    db=Mock();db.execute.return_value.fetchone.return_value={'status':{'scope':'original-import','elapsed_base':120,'started':'2026-10-13T10:00:00+00:00','updated':'2026-10-13T10:03:00+00:00'}}
    assert elapsed_base(db,3,'metadata')==300


def test_duplicate_only_handoffs_skip_both_workers(tmp_path):
    db=Mock();db.execute.return_value.fetchone.return_value={'state':'completed-review-required','auto_metadata':True,'auto_conversion':True}
    @contextmanager
    def connection():yield db
    with patch.object(pipeline,'DATA',tmp_path),patch.object(pipeline,'connection',connection),patch.object(pipeline,'idle'),patch('app.jobs.job_scope.require_import_complete'),patch('app.jobs.job_scope.pending_work',return_value=False),patch.object(videos,'run') as metadata,patch.object(conversions,'run') as conversion:
        pipeline.run(2,'metadata')
        metadata.assert_not_called();conversion.assert_not_called()
    assert json.loads((tmp_path/'pipeline-status.json').read_text())['state']=='completed'


def test_shared_scope_timing_is_not_attributed_to_original_work():
    from app.jobs.job_scope import original_status
    old={'scope':'import','elapsed_base':120,'started':'2026-10-13T10:00:00+00:00','updated':'2026-10-13T10:03:00+00:00'}
    db=Mock();db.execute.return_value.fetchone.return_value={'status':old}
    assert elapsed_base(db,3,'metadata')==0 and original_status(old) is None


def test_auto_scan_follows_conversion_in_same_import(tmp_path):
    db=Mock();db.execute.return_value.fetchone.return_value={'state':'completed-review-required','auto_metadata':True,'auto_conversion':True,'auto_duplicates':True}
    @contextmanager
    def connection():yield db
    def conversion(**kw):
        (tmp_path/'avi-status.json').write_text(json.dumps({'state':'completed-review-required'}))
    def scan(job_id):
        assert job_id==4
        (tmp_path/'photo-duplicate-status.json').write_text(json.dumps({'state':'completed-review-required'}))
    with patch.object(pipeline,'DATA',tmp_path),patch.object(pipeline,'connection',connection),patch.object(pipeline,'idle'),patch('app.jobs.job_scope.require_import_complete'),patch('app.jobs.job_scope.pending_work',return_value=True),patch.object(conversions,'run',conversion),patch('app.photos.duplicates.run',side_effect=scan) as duplicate:
        pipeline.run(4,'conversion')
        duplicate.assert_called_once_with(4)
    assert json.loads((tmp_path/'pipeline-status.json').read_text())['state']=='completed'


def test_options_save_normalizes_conversion_and_never_launches():
    import asyncio
    from unittest.mock import AsyncMock
    from app.imports import api
    db=Mock();db.execute.return_value.fetchone.return_value={'id':4}
    @contextmanager
    def connection():yield db
    request=Mock();request.json=AsyncMock(return_value={'auto_metadata':False,'auto_conversion':True,'auto_duplicates':True})
    with patch.object(api,'connection',connection),patch.object(pipeline,'launch') as launch:
        asyncio.run(api.import_options(4,request))
        launch.assert_not_called()
    assert db.execute.call_args.args[1]==(True,True,True,4)
