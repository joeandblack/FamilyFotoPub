import json

import pytest

from app.backup.postgres import Endpoint
from app.backup.recovery import sanitize_files, sanitize_database
from app.backup.policy import PAUSE_MARKERS


def test_recovered_settings_block_normal_startup_and_pause_faces(tmp_path, monkeypatch):
    from app import config
    files = tmp_path / 'files'
    (files / 'config').mkdir(parents=True)
    (files / 'config/local.json').write_text('{"postgres_admin_password":"old-admin-secret"}')
    job = files / 'data/face_detect/profiles' / ('a' * 32) / 'jobs/import-1'
    job.mkdir(parents=True)
    (job / 'raw.json').write_text('{"state":"running","items":[]}')
    (job / 'checkpoint.json').write_text('{"state":"queued"}')
    (files / 'data/pipeline-status.json').write_text('{"state":"running","done":12}')
    (files / 'data/avi-status.json').write_text('{"state":"paused-error","error":"review original"}')
    sanitize_files(files, Endpoint('127.0.0.1', 55489, 'fixture', 'recovery'), 'test-capture')
    assert 'postgres_admin_password' not in json.loads((files / 'config/local.json').read_text())
    assert json.loads((job / 'raw.json').read_text())['state'] == 'paused'
    assert json.loads((job / 'checkpoint.json').read_text())['state'] == 'paused'
    assert json.loads((job / 'control.json').read_text()) == {'action': 'pause'}
    for name in PAUSE_MARKERS:
        assert (files / 'data' / name).exists()
    pipeline = json.loads((files / 'data/pipeline-status.json').read_text())
    assert pipeline['state'] == 'paused'
    assert pipeline['recovery_previous_state'] == 'running'
    assert pipeline['done'] == 12
    fault = json.loads((files / 'data/avi-status.json').read_text())
    assert fault['state'] == 'paused-error'
    assert fault['error'] == 'review original'
    monkeypatch.setattr(config, 'ROOT', files)
    with pytest.raises(RuntimeError, match='validation-only'):
        config.load_config()


def test_sanitation_refuses_normal_database_before_connecting(monkeypatch):
    import psycopg
    monkeypatch.setattr(psycopg, 'connect', lambda **kwargs: pytest.fail('Must not connect'))
    with pytest.raises(ValueError, match='restricted'):
        sanitize_database(Endpoint('127.0.0.1', 55489, 'familyfoto', 'user'))
