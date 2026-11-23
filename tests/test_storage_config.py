import copy
import json
from pathlib import Path
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
from app.storage import config as sc
from app.imports.manual import source_folder


@pytest.fixture
def setup(tmp_path,monkeypatch):
    # These fixtures model legacy Windows drive identities.
    monkeypatch.setattr(sc, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr('app.storage.disks.sys', SimpleNamespace(platform='win32'))
    source=tmp_path/'photos';source.mkdir()
    destination=tmp_path/'library'/'media';destination.mkdir(parents=True)
    def identity(path):return {'root':str(path),'drive':path.drive.rstrip(':'),'serial':'disk','volume_id':'volume','volume_serial':'ABC'}
    config={'source':identity(source),'destination':identity(destination),'database':{'password':'fixture-secret'},'browser_password':'fixture-secret'}
    path=tmp_path/'local.json';path.write_text(json.dumps(config))
    monkeypatch.setattr(sc,'CONFIG_PATH',path)
    monkeypatch.setattr(sc,'ROOT',tmp_path/'application')
    monkeypatch.setattr(sc,'load_config',lambda:json.loads(path.read_text()))
    monkeypatch.setattr(sc,'snapshot',lambda drives:{d:{**identity(source),'online':True} for d in drives})
    monkeypatch.setattr('app.jobs.idle',lambda db:None)
    monkeypatch.setattr('app.jobs.snapshot',lambda db:{'busy':False})
    db=Mock();db.execute.return_value.fetchone.return_value={'populated':False};db.execute.return_value.fetchall.return_value=[]
    db.execute.side_effect=lambda query,*args:Mock(fetchone=lambda:None) if query.startswith('SELECT source_root,source_storage') else db.execute.return_value
    return config,path,db


def payload(config):return {'source':config['source']['root'],'destination':config['destination']['root'],'revision':sc.revision()}


def test_save_source_preserves_credentials_and_legacy_identity(setup,tmp_path):
    config,path,db=setup;original=copy.deepcopy(config)
    new=tmp_path/'another-source';new.mkdir()
    job={'id':4,'source_root':str(Path(config['source']['root'])/'old-import')}
    db.execute.return_value.fetchall.return_value=[job]
    value=payload(config);value['source']=str(new)
    sc.save(db,value);saved=json.loads(path.read_text())
    assert saved['source']['root']==str(new)
    assert saved['database']==original['database'] and saved['destination']==original['destination']
    call=next(c for c in db.execute.call_args_list if c.args[0].startswith('UPDATE jobs'))
    assert call.args[1][0].obj=={**original['source'],'root':job['source_root']}
    assert call.args[1][1]==4


def test_import_resume_uses_pinned_identity_after_default_changes(setup):
    config,_,_=setup
    pinned=copy.deepcopy(config['source']);pinned['serial']='original-disk'
    config['source']['serial']='new-default-disk'
    result=sc.import_config(config,{'source_root':pinned['root'],'source_storage':pinned})
    assert result['source']['serial']=='original-disk'
    assert config['source']['serial']=='new-default-disk'


def test_latest_source_is_read_only_and_uses_pinned_offline_identity(setup):
    config,path,db=setup;before=path.read_bytes()
    pinned={**config['source'],'root':r'G:\offline\latest','drive':'G','serial':'other-disk'}
    db.execute.side_effect=None
    db.execute.return_value.fetchone.return_value={'source_root':pinned['root'],'source_storage':pinned}
    assert sc.latest_source(db,config)==pinned
    assert "state<>'cancelled' AND source_kind='server' ORDER BY id DESC" in db.execute.call_args.args[0]
    assert path.read_bytes()==before


def test_latest_source_empty_and_legacy_fallback(setup):
    config,_,db=setup
    assert sc.latest_source(db,config)==config['source']
    db.execute.side_effect=None
    root=str(Path(config['source']['root'])/'latest-offline')
    db.execute.return_value.fetchone.return_value={'source_root':root,'source_storage':None}
    assert sc.latest_source(db,config)=={**config['source'],'root':root}
    db.execute.return_value.fetchone.return_value={'source_root':r'Z:\unknown','source_storage':None}
    assert sc.latest_source(db,config)=={'root':r'Z:\unknown','kind':'unverified'}


def test_populated_destination_change_is_blocked_without_writes(setup,tmp_path):
    config,path,db=setup;before=path.read_bytes()
    db.execute.return_value.fetchone.return_value={'populated':True}
    new=tmp_path/'new';new.mkdir()
    value=payload(config);value['destination']=str(new)
    with pytest.raises(ValueError,match='migration'):sc.save(db,value)
    assert path.read_bytes()==before and list(new.iterdir())==[]


def test_empty_library_can_enroll_destination_without_touching_it(setup,tmp_path):
    config,path,db=setup;new=tmp_path/'new';new.mkdir()
    value=payload(config);value['destination']=str(new)
    sc.save(db,value)
    assert json.loads(path.read_text())['destination']['root']==str(new)
    assert list(new.iterdir())==[]


def test_busy_and_stale_settings_fail_before_disk_changes(setup,monkeypatch):
    config,path,db=setup;before=path.read_bytes()
    with pytest.raises(ValueError,match='refresh'):sc.save(db,{**payload(config),'revision':'stale'})
    def busy(db):raise RuntimeError('Another operation is active')
    monkeypatch.setattr('app.jobs.idle',busy)
    with pytest.raises(RuntimeError,match='active'):sc.save(db,payload(config))
    assert path.read_bytes()==before


def test_overlapping_and_nonempty_destinations_refused(setup,tmp_path):
    config,path,db=setup;before=path.read_bytes()
    new=tmp_path/'occupied';new.mkdir();(new/'keep.jpg').write_bytes(b'unchanged')
    with pytest.raises(ValueError,match='empty'):sc.save(db,{**payload(config),'destination':str(new)})
    with pytest.raises(ValueError,match='separate'):source_folder(config,config['destination']['root'])
    assert path.read_bytes()==before and (new/'keep.jpg').read_bytes()==b'unchanged'


def test_selected_local_source_enrolls_its_drive_not_old_default(setup,monkeypatch):
    config,_,_=setup;config['source']['drive']='Z'
    read=Mock(return_value={Path(config['source']['root']).drive.rstrip(':'):{'online':True,'serial':'chosen','volume_id':'chosen-volume','volume_serial':'123'}})
    monkeypatch.setattr(sc,'snapshot',read)
    result=sc.enroll_source(config,config['source']['root'])
    assert result['serial']=='chosen'
    read.assert_called_once_with([Path(config['source']['root']).drive.rstrip(':')])


def test_network_source_enrollment_never_queries_a_local_disk(setup,monkeypatch):
    config,_,_=setup
    monkeypatch.setattr(sc,'source_folder',lambda c,p:Path(r'\\server\photos\album'))
    query=Mock();monkeypatch.setattr(sc,'snapshot',query)
    identity=sc.enroll_source(config,r'\\server\photos\album')
    assert identity['kind']=='network' and identity['drive'] is None
    query.assert_not_called()


def test_network_monitor_still_verifies_destination(setup,monkeypatch):
    from app.storage import disks
    config,_,_=setup
    config['source']={'kind':'network','root':r'\\server\photos\album','drive':None}
    expected=config['destination']
    query=Mock(return_value={expected['drive']:{**expected,'online':True}})
    monkeypatch.setattr(disks,'snapshot',query)
    monkeypatch.setattr(disks,'logger',lambda name:Mock())
    validate=Mock();monkeypatch.setattr('app.imports.manual.source_folder',validate)
    monitor=disks.Monitor(config)
    result=monitor.verify()
    assert result['network-source']['online']
    query.assert_called_once_with([expected['drive']],monitor.since)
    validate.assert_called_once_with(config,config['source']['root'])


@pytest.mark.parametrize('path',[r'\\?\C:\photos',r'\\.\C:\photos'])
def test_device_paths_cannot_bypass_volume_identity(setup,path):
    config,_,_=setup
    with pytest.raises(ValueError,match='Device paths'):source_folder(config,path)


def test_setup_accepts_explicit_paths_without_touching_media(tmp_path,monkeypatch):
    from installer import configure
    monkeypatch.setattr(configure.getpass,'getpass',lambda *_:'fixture-password-only')
    (tmp_path/'app').mkdir()
    source=tmp_path/'arbitrary-source';source.mkdir()
    destination=tmp_path/'new-library'/'media'
    identity={'root':str(source),'drive':source.drive.rstrip(':'),'serial':'fixture','volume_id':'fixture','volume_serial':'ABC'}
    monkeypatch.setattr(configure,'ROOT',tmp_path/'app')
    monkeypatch.setattr(configure.sys,'argv',['configure.py','--source',str(source),'--destination',str(destination)])
    monkeypatch.setattr(sc,'enroll_source',lambda config,value:identity)
    monkeypatch.setattr('app.storage.disks.snapshot',lambda drives:{d:{**identity,'online':True} for d in drives})
    configure.main()
    saved=json.loads((tmp_path/'app'/'config'/'local.json').read_text())
    assert saved['source']['root']==str(source) and saved['destination']['root']==str(destination)
    assert not destination.exists() and list(source.iterdir())==[]


def test_setup_defers_storage_without_querying_disks(tmp_path,monkeypatch):
    from installer import configure
    monkeypatch.setattr(configure,'ROOT',tmp_path)
    monkeypatch.setattr(configure.sys,'argv',['configure.py'])
    monkeypatch.setattr(configure.getpass,'getpass',lambda *_:'fixture-password-only')
    disk=Mock(side_effect=AssertionError('Setup must not inspect media disks'))
    monkeypatch.setattr('app.storage.disks.snapshot',disk)
    configure.main()
    saved=json.loads((tmp_path/'config'/'local.json').read_text())
    assert saved['source']==saved['destination']=={}
    assert saved['browser_password_hash'] and 'browser_password' not in saved
    disk.assert_not_called()


def test_first_import_enrolls_storage_without_source_for_upload(setup,tmp_path):
    config,path,db=setup
    config['source']={};config['destination']={}
    path.write_text(json.dumps(config))
    assert sc.settings(db)['destination']==''
    destination=tmp_path/'new-media'
    sc.save(db,{'source':'','destination':str(destination),'revision':sc.revision()})
    saved=json.loads(path.read_text())
    assert saved['source']=={}
    assert saved['destination']['root']==str(destination)
    assert saved['database']==config['database']
    assert not destination.exists()


def test_unconfigured_destination_blocks_upload_before_staging(monkeypatch):
    from app import uploads
    monkeypatch.setattr(uploads,'load_config',lambda:{'source':{},'destination':{}})
    with pytest.raises(ValueError,match='Choose the library destination'):
        uploads.create('fixture',{})


def test_source_selection_allowed_before_storage_enrollment(tmp_path):
    assert source_folder({'destination':{}},str(tmp_path))==tmp_path.resolve()
