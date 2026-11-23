import plistlib
from types import SimpleNamespace
from pathlib import Path
import pytest
from app.storage import macos_disks as mac
from app.storage import disks

VOLUME = '098BE919-C782-379F-9ECD-1DFF7023A4E0'
PARTITION = '11131CF3-E394-46AC-AA25-C36D8A0304B1'


def directory_symlink(link,target):
    try:link.symlink_to(target,target_is_directory=True)
    except OSError as error:
        if getattr(error,'winerror',None)==1314:
            pytest.skip('Windows account lacks symlink privilege; native macOS symlink test not run')
        raise


@pytest.fixture
def volume(tmp_path, monkeypatch):
    mount = tmp_path / 'Disk with spaces'; mount.mkdir()
    raw = dict(VolumeUUID=VOLUME, DiskUUID=PARTITION, MountPoint=str(mount),
               FilesystemType='exfat', WritableVolume=True, DeviceIdentifier='disk4s1')
    monkeypatch.setattr(mac.os.path, 'ismount', lambda p: Path(p) == mount)
    monkeypatch.setattr(mac.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0,stdout=plistlib.dumps(raw)))
    return mount, raw


def test_uuid_identity_not_device_number(volume):
    mount, raw = volume
    first = mac.snapshot([str(mount)])[str(mount)]
    raw['DeviceIdentifier'] = 'disk9s1'
    later = mac.snapshot([str(mount)])[str(mount)]
    disks.match_disk(first, later)
    assert first['volume_serial'] == VOLUME
    assert first['serial'] == PARTITION


def test_same_name_replacement_refused(volume):
    mount, raw = volume
    expected = {**mac.snapshot([str(mount)])[str(mount)], 'root':str(mount/'media')}
    raw['VolumeUUID'] = 'E2589CCB-D45A-447F-AC54-D150697AC2BC'
    with pytest.raises(disks.DiskError, match='identity mismatch'):
        mac.verify_path(expected, writable=True)


def test_missing_mount_never_falls_back_to_local_directory(volume, monkeypatch):
    mount, raw = volume
    raw['MountPoint'] = str(mount.parent)  # A stale directory on the system volume.
    with pytest.raises(disks.DiskError, match='Mount point changed'):
        mac.snapshot([str(mount)])


def test_readonly_destination_rejected(volume):
    mount, raw = volume
    expected = {**mac.snapshot([str(mount)])[str(mount)], 'root':str(mount/'media')}
    raw['WritableVolume'] = False
    with pytest.raises(disks.DiskError, match='read-only'):
        mac.verify_path(expected, writable=True)
    mac.verify_path(expected, writable=False)


def test_missing_uuid_rejected(volume):
    mount, raw = volume
    raw.pop('VolumeUUID')
    with pytest.raises(disks.DiskError, match='UUIDs'):
        mac.snapshot([str(mount)])


def test_linked_paths_rejected(volume):
    mount, _ = volume
    alias = mount/'alias'; directory_symlink(alias,mount)
    with pytest.raises(disks.DiskError, match='Linked'):
        mac.existing_parent(alias/'photo')


def test_apfs_firmlink_uses_df_device(volume, monkeypatch):
    mount, raw = volume
    calls=[]
    def run(args, **kwargs):
        calls.append(args)
        if args[0] == '/bin/df':
            return SimpleNamespace(returncode=0,stdout='Filesystem blocks used free capacity mounted\n/dev/disk3s5 1 1 1 10% /System/Volumes/Data\n')
        return SimpleNamespace(returncode=0,stdout=plistlib.dumps(raw))
    monkeypatch.setattr(mac.subprocess,'run',run)
    assert mac.drive_for_path(mount/'new folder') == str(mount)
    assert calls[1][-1] == '/dev/disk3s5'


def test_stop_blocks_new_pipeline_work(monkeypatch, tmp_path):
    from app import jobs as pipeline
    from app.service import macos_lifecycle as lifecycle
    monkeypatch.setattr(lifecycle.sys, 'platform', 'darwin')
    monkeypatch.setattr(lifecycle, 'STOP', tmp_path/'STOP')
    monkeypatch.setattr(lifecycle, 'DATA', tmp_path)
    lifecycle.request_pause()
    assert all((tmp_path/name).exists() for name in lifecycle.PAUSES)
    with pytest.raises(RuntimeError, match='stopping'):
        pipeline.launch(1,'import')
    with pytest.raises(RuntimeError, match='stopping'):
        pipeline.run(1,'import')


def test_shutdown_cancels_unstarted_handoff(monkeypatch, tmp_path):
    import asyncio
    from contextlib import contextmanager
    from unittest.mock import Mock
    from app import database
    from app.service import macos_lifecycle
    from app.jobs import work_admission
    db = Mock()
    db.execute.return_value.fetchone.return_value = {'busy':False}
    @contextmanager
    def connection():
        yield db
    monkeypatch.setattr(database, 'connection', connection)
    monkeypatch.setattr(macos_lifecycle, 'DATA', tmp_path)
    monkeypatch.setattr(work_admission, 'read', lambda: {'state':'pending','token':'fixture'})
    finish = Mock()
    monkeypatch.setattr(work_admission, 'finish', finish)
    asyncio.run(macos_lifecycle.drain_workers())
    assert finish.call_args.args[:3] == (db, 'fixture', 'cancelled')


@pytest.mark.parametrize('failure',['replacement','missing','stale','readonly','symlink','nested'])
def test_fast_guard_fails_closed(volume,monkeypatch,failure):
    mount,_=volume
    expected={'drive':str(mount),'root':str(mount/'media'),'volume_serial':VOLUME,'volume_id':VOLUME}
    monkeypatch.setattr(mac,'volume_attributes',lambda path:(VOLUME,True,str(mount)))
    if failure=='replacement':monkeypatch.setattr(mac,'volume_attributes',lambda path:(PARTITION,True,str(mount)))
    if failure=='missing':mount.rmdir()
    if failure=='stale':monkeypatch.setattr(mac,'volume_attributes',lambda path:(VOLUME,True,str(mount.parent)))
    if failure=='readonly':monkeypatch.setattr(mac,'volume_attributes',lambda path:(VOLUME,False,str(mount)))
    if failure=='symlink':directory_symlink(mount/'media',mount)
    if failure=='nested':
        monkeypatch.setattr(mac,'existing_parent',lambda path:SimpleNamespace(stat=lambda:SimpleNamespace(st_dev=-1)))
    with pytest.raises(disks.DiskError):mac.fast_verify_path(expected,writable=True)


def test_fast_guard_queries_live_identity_every_time_without_subprocess(volume,monkeypatch):
    mount,_=volume;calls=[]
    expected={'drive':str(mount),'root':str(mount/'media'),'volume_serial':VOLUME,'volume_id':VOLUME}
    def native(path):calls.append(path);return (VOLUME if len(calls)==1 else PARTITION),True,str(mount)
    monkeypatch.setattr(mac,'volume_attributes',native)
    def forbidden(*args,**kwargs):raise AssertionError('No subprocess in chunk guard')
    monkeypatch.setattr(mac.subprocess,'run',forbidden)
    mac.fast_verify_path(expected,writable=True)
    with pytest.raises(disks.DiskError,match='identity mismatch'):mac.fast_verify_path(expected,writable=True)
    assert len(calls)==2


def test_monitor_fast_guard_retains_heartbeat_and_marker_checks(volume,tmp_path,monkeypatch):
    import time
    import json
    mount,_=volume
    expected={'drive':str(mount),'root':str(mount/'media'),'volume_serial':VOLUME,'volume_id':VOLUME}
    monkeypatch.setattr(disks.sys,'platform','darwin')
    monkeypatch.setattr(mac,'volume_attributes',lambda path:(VOLUME,True,str(mount)))
    monitor=disks.Monitor({'destination':expected},roles=('destination',))
    monitor.marker_id='owned';monitor.marker.write_text(json.dumps({'library_id':'owned'}));monitor.last_ok=time.monotonic()
    monitor.check()
    monitor.marker.write_text(json.dumps({'library_id':'replacement'}))
    with pytest.raises(disks.DiskError,match='marker changed'):monitor.check()
    monitor.last_ok=time.monotonic()-61
    with pytest.raises(disks.DiskError,match='heartbeat expired'):monitor.check()
    monitor.error='lost partition'
    with pytest.raises(disks.DiskError,match='lost partition'):monitor.check()
