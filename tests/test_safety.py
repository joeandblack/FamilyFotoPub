import hashlib
import sys
from pathlib import Path
import pytest
from app.storage.disks import match_disk,DiskError
from app.storage import disks
import json
import time
from app.library.media import TrafficLimit,copy_verified,digest,safe_media_path,kind_for
from app.telemetry import StageTimings
from app.imports import importer

def test_progress_is_throttled_and_terminal_update_forced(tmp_path,monkeypatch):
    clock=[10.0]
    monkeypatch.setattr(importer.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(importer,'DATA',tmp_path)
    updates=[]
    monkeypatch.setattr(importer,'publish',lambda *args:updates.append(args))
    db=object()
    reporter=importer.ProgressReporter(db,1,StageTimings())
    reporter.update('first')
    clock[0]=14.9
    reporter.update('second')
    assert len(updates)==1
    clock[0]=15.0
    reporter.update('third')
    reporter.update(force=True)
    assert len(updates)==3
    assert all(args[2] is db for args in updates)
    assert json.loads((tmp_path/'performance.json').read_text())['calls']['progress_reporting']==3

def test_identity_requires_all_identifiers():
    expected={'drive':'E','serial':'expected','volume_serial':'A123','volume_id':'guid'}
    match_disk(expected,{**expected,'serial':' expected '})
    for field in ('serial','volume_serial','volume_id'):
        with pytest.raises(DiskError):
            match_disk(expected,{**expected,field:'wrong'})

def test_copy_never_changes_source(tmp_path):
    source=tmp_path/'source'/'photo.jpg';source.parent.mkdir()
    source.write_bytes(b'original photo'*1000)
    before=(source.read_bytes(),source.stat().st_mtime_ns)
    target=tmp_path/'destination'/'photo.jpg'
    guard=lambda:None
    limit=TrafficLimit(0)
    sha=digest(source,guard,limit)
    timings=StageTimings()
    copy_verified(source,target,sha,guard,limit,timings)
    assert {'copy_source_read','destination_write','destination_flush','destination_verify'}<=timings.seconds.keys()
    assert (source.read_bytes(),source.stat().st_mtime_ns)==before
    assert target.read_bytes()==before[0]

def test_pause_leaves_original_and_no_completed_copy(tmp_path):
    source=tmp_path/'original.jpg';source.write_bytes(b'x'*100)
    target=tmp_path/'copy.jpg'
    calls=[]
    def guard():
        calls.append(True)
        if len(calls)>1:
            raise DiskError('disconnected')
    with pytest.raises(DiskError):
        copy_verified(source,target,hashlib.sha256(source.read_bytes()).hexdigest(),guard,TrafficLimit(0))
    assert source.read_bytes()==b'x'*100
    assert not target.exists()

def test_checksum_failure_does_not_publish_copy(tmp_path):
    source=tmp_path/'original.jpg';source.write_bytes(b'original')
    target=tmp_path/'copy.jpg'
    with pytest.raises(OSError,match='verification failed'):
        copy_verified(source,target,'wrong',lambda:None,TrafficLimit(0))
    assert not target.exists()
    assert source.read_bytes()==b'original'

def test_path_and_classification(tmp_path):
    with pytest.raises(ValueError):
        safe_media_path(tmp_path,'../outside.jpg')
    assert kind_for('PHOTO.JPG')=='image'
    assert kind_for('clip.MOV')=='video'
    assert kind_for('notes.txt') is None

@pytest.mark.skipif(sys.platform=='darwin',reason='Windows drive-letter monitor contract; macOS is covered by test_macos_disks')
def test_volume_swap_marker_swap_and_stale_monitor(tmp_path,monkeypatch):
    config={'source':{'drive':'F','volume_serial':'SOURCE'},'destination':{'drive':'E','volume_serial':'DEST','root':str(tmp_path/'media')}}
    monitor=disks.Monitor(config)
    monitor.marker=tmp_path/'marker.json'
    monitor.marker_id='library'
    monitor.marker.write_text(json.dumps({'library_id':'library'}))
    monitor.last_ok=time.monotonic()
    monkeypatch.setattr(disks,'volume_serial',lambda d:'SOURCE' if d=='F' else 'DEST')
    monitor.check()
    monkeypatch.setattr(disks,'volume_serial',lambda d:'WRONG')
    with pytest.raises(DiskError,match='volume changed'):
        monitor.check()
    monkeypatch.setattr(disks,'volume_serial',lambda d:'SOURCE' if d=='F' else 'DEST')
    monitor.marker.write_text(json.dumps({'library_id':'another-library'}))
    with pytest.raises(DiskError,match='marker changed'):
        monitor.check()
    monitor.last_ok=time.monotonic()-61
    with pytest.raises(DiskError,match='heartbeat expired'):
        monitor.check()


@pytest.mark.skipif(sys.platform=='darwin',reason='Windows drive-letter monitor contract; macOS is covered by test_macos_disks')
def test_video_monitor_does_not_access_source_but_requires_destination(tmp_path,monkeypatch):
    config={'source':{'drive':'F'},'destination':{'drive':'E','volume_serial':'DEST','root':str(tmp_path/'media')}}
    monitor=disks.Monitor(config,roles=('destination',))
    monitor.marker=tmp_path/'marker.json';monitor.marker_id='library'
    monitor.marker.write_text(json.dumps({'library_id':'library'}))
    monitor.last_ok=time.monotonic()
    calls=[]
    def serial(drive):
        calls.append(drive)
        assert drive=='E'
        return 'DEST'
    monkeypatch.setattr(disks,'volume_serial',serial)
    monitor.check();assert calls==['E']
    with pytest.raises(ValueError): disks.Monitor(config,roles=('source',))
