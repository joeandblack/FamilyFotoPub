from collections import namedtuple
from app import storage
from types import SimpleNamespace
import pytest

@pytest.fixture(autouse=True)
def windows_volume_fixtures(monkeypatch):
    monkeypatch.setattr(storage, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr("app.storage.disks.sys", SimpleNamespace(platform="win32"))



def test_capacity_is_per_disk_and_read_only(monkeypatch):
    config={role:{'drive':drive,'root':drive+':/media','serial':'serial','volume_serial':'ABC','volume_id':'id'}
            for role,drive in [('source','F'),('destination','E')]}
    calls=[]
    monkeypatch.setattr(storage,'snapshot',lambda drives:{drives[0]:{**config['source'],'online':True}})
    monkeypatch.setattr(storage,'volume_serial',lambda drive:'ABC')
    usage=namedtuple('Usage','total used free')(1000,600,400)
    monkeypatch.setattr(storage.shutil,'disk_usage',lambda path:(calls.append(path) or usage))
    result=storage.capacity(config)
    assert result['source']['total']==1000 and result['destination']['used']==600
    assert calls==['F:\\','E:\\']
    result=storage.capacity(config,'F:\\PHOTOS\\ME')
    assert result['source']['root']=='F:\\PHOTOS\\ME'
    assert config['source']['root']=='F:/media'
    def offline(drives):
        if drives==['F']:raise OSError('Offline')
        return {'E':{**config['destination'],'online':True}}
    monkeypatch.setattr(storage,'snapshot',offline)
    result=storage.capacity(config)
    assert result['source']['error']=='Offline'
    assert result['destination']['free']==400


def test_wrong_disk_does_not_report_capacity(monkeypatch):
    config={role:{'drive':'E','root':'E:/media','serial':'expected','volume_serial':'ABC','volume_id':'id'} for role in ('source','destination')}
    monkeypatch.setattr(storage,'snapshot',lambda drives:{'E':{'serial':'wrong'}})
    monkeypatch.setattr(storage.shutil,'disk_usage',lambda path:(_ for _ in ()).throw(AssertionError('Wrong disk read')))
    assert all('error' in storage.capacity(config)[role] for role in config)


def test_offline_drive_message_hides_powershell_trace(monkeypatch):
    from types import SimpleNamespace
    from app.storage import disks
    import pytest
    monkeypatch.setattr(disks.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=1,stderr=b'Get-Partition: CmdletizationQuery_NotFound_DriveLetter,Get-Partition'))
    with pytest.raises(disks.DiskError,match='Selected disk is disconnected') as error:disks.snapshot(['F'])
    assert 'Get-Partition' not in str(error.value)
