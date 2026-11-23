from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient
from app.api import app
from app.video.handoff import run

def test_legacy_handoff_cannot_start_workers():
    with patch('app.video.metadata.run') as worker:
        with pytest.raises(RuntimeError,match='retired'):run(8,True)
        worker.assert_not_called()

@pytest.mark.parametrize('route',['videos','conversions','import'])
def test_global_resume_retired(route,tmp_path,monkeypatch):
    monkeypatch.setattr('app.auth.api.session_user',lambda *_:{'username':'fixture','role':'admin'})
    for name in ('PAUSE','VIDEO-PAUSE','AVI-PAUSE'):(tmp_path/name).touch()
    with patch('app.imports.api.DATA',tmp_path),patch('app.auth.api.load_config',return_value={'host':'localhost'}),patch('app.imports.api.load_config',return_value={'host':'localhost'}),patch('subprocess.Popen') as launch:
        assert TestClient(app).post(f'/api/{route}/resume').status_code==409
        launch.assert_not_called()
    assert all((tmp_path/n).exists() for n in ('PAUSE','VIDEO-PAUSE','AVI-PAUSE'))
