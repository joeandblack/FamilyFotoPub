import io
import json
import tarfile

import pytest

from app.backup.encrypted import InvalidArtifact, seal_stream
from app.backup.postgres import Endpoint
from app.backup.recovery import sanitize_files, save_private
from app.backup.state_files import restore_state, STATE_KIND
from app.migration.state_validation import validate_file_state
from tests.test_backup_postgres import ample
from tests.backup_keys import backup_key


@pytest.fixture
def recovered(tmp_path):
    profile = 'data/face_detect/profiles/' + 'a' * 32
    job = profile + '/jobs/example'
    records = {
        'config/local.json': {'auth_secret': 'old', 'session_epoch': 'old', 'postgres_admin_password': 'old',
                              'destination': {'path': 'old-machine'}, 'custom_setting': 'preserved'},
        'data/people-preview.json': {'people': [{'name': 'Example', 'id': 1}]},
        'data/face-service-settings.json': {'start_with_app': True},
        'data/import-status.json': {'state': 'running', 'done': 5},
        'data/video-status.json': {'state': 'complete', 'done': 2},
        job + '/raw.json': {'state': 'running', 'decisions': [1, 2]},
        job + '/checkpoint.json': {'state': 'complete', 'processed': 3},
        job + '/import.json': {'state': 'waiting-service', 'import': 3},
    }
    raw = io.BytesIO()
    total = 0
    with tarfile.open(fileobj=raw, mode='w') as archive:
        for name, value in records.items():
            data = json.dumps(value).encode()
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            total += len(data)
            archive.addfile(entry, io.BytesIO(data))
    raw.seek(0)
    artifact, files, media = (tmp_path / name for name in ('artifact', 'files', 'media'))
    key = backup_key()
    seal_stream(raw, artifact, key, estimated_bytes=len(raw.getvalue()), guard_factory=ample,
                metadata={'kind': STATE_KIND, 'file_count': len(records), 'file_bytes': total})
    restore_state(artifact, key, files, guard_factory=ample)
    endpoint = Endpoint('127.0.0.1', 55489, 'fixture', 'fixture')
    sanitize_files(files, endpoint, 'b' * 32)
    config = json.loads((files / 'config/local.json').read_text())
    config['recovery'].update(media_verified=True, media_root=str(media), media_inventory_sha256='c' * 64)
    save_private(files / 'config/local.json', config)
    def validate():
        return validate_file_state(artifact, key, files, endpoint, 'b' * 32, media, 'c' * 64)
    return validate, files, job


def test_sanitized_state_keeps_decisions_but_never_replays_controls(recovered):
    validate, files, job = recovered
    assert validate()['state'] == 'verified-sanitized-state'
    assert json.loads((files / job / 'raw.json').read_text()) == {'state': 'paused', 'decisions': [1, 2]}


@pytest.mark.parametrize('change', ['person', 'job', 'checkpoint', 'control', 'missing', 'extra',
                                   'secret', 'custom', 'inventory', 'database', 'service', 'report', 'pause'])
def test_sanitized_state_rejects_unapproved_changes(recovered, change):
    validate, files, job = recovered
    paths = {'person': 'data/people-preview.json', 'job': job + '/raw.json',
             'checkpoint': job + '/checkpoint.json', 'control': job + '/control.json',
             'service': 'data/face-service-settings.json', 'report': 'RESTORE-STATE-ONLY.json'}
    if change in paths:
        path = files / paths[change]
        value = json.loads(path.read_text())
        value['unexpected'] = True
        path.write_text(json.dumps(value))
    elif change == 'missing':
        (files / 'data/people-preview.json').unlink()
    elif change == 'extra':
        (files / 'data/run-now.json').write_text('{}')
    elif change == 'pause':
        (files / 'data/PAUSE').write_bytes(b'x')
    else:
        path = files / 'config/local.json'
        value = json.loads(path.read_text())
        if change == 'secret':
            value['auth_secret'] = 'old'
        elif change == 'custom':
            value['custom_setting'] = 'altered'
        elif change == 'inventory':
            value['recovery']['media_inventory_sha256'] = 'd' * 64
        else:
            value['database']['options'] = '-c default_transaction_read_only=off'
        path.write_text(json.dumps(value))
    with pytest.raises((InvalidArtifact, FileNotFoundError)):
        validate()
