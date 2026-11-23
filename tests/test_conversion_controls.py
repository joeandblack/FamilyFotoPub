from contextlib import contextmanager
from unittest.mock import patch
from fastapi.testclient import TestClient
from app.api import app
from app.video import conversions
import pytest


@pytest.mark.parametrize('available,pending,metadata',[(False,0,0),(True,1,0),(True,0,1)])
def test_busy_or_outstanding_work_blocks(tmp_path,monkeypatch,available,pending,metadata):
    monkeypatch.setattr(conversions,'DATA',tmp_path)
    class DB:
        def execute(self,query,args=None):
            self.result={'locked':available} if 'advisory_lock' in query else {'count':metadata if 'LEFT JOIN' in query else pending}
            return self
        def fetchone(self): return self.result
        def commit(self): pass
    with pytest.raises(RuntimeError): conversions.check_ready(DB())


def test_manual_resume_and_pause_only_touch_conversion_flag(tmp_path,monkeypatch):
    monkeypatch.setattr('app.auth.api.session_user',lambda *_:{'username':'fixture','role':'admin'})
    config={'host':'localhost','avi_conversion':{'enabled':True}}
    (tmp_path/'VIDEO-PAUSE').touch()
    (tmp_path/'AVI-PAUSE').touch()
    @contextmanager
    def connection(): yield object()
    with patch('app.imports.api.DATA',tmp_path),patch('app.auth.api.load_config',return_value=config),patch('app.imports.api.load_config',return_value=config),patch('app.imports.api.connection',connection),patch('app.video.conversions.check_ready'),patch('app.video.conversions.save_parameters'),patch('subprocess.Popen') as spawn:
        client=TestClient(app)
        assert client.post('/api/conversions/resume').status_code==409
        spawn.assert_not_called()
        assert (tmp_path/'VIDEO-PAUSE').exists()
        assert (tmp_path/'AVI-PAUSE').exists()
        assert client.post('/api/conversions/pause').status_code==200
        assert (tmp_path/'AVI-PAUSE').exists()
