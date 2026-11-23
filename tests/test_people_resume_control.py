import json
from contextlib import nullcontext

from app.telemetry import write_json
from face_detect.jobs import automatic_people_import as batches
from face_detect.jobs.automatic_people_jobs import Jobs


def test_resume_clears_only_pending_paused_children(tmp_path, monkeypatch):
    monkeypatch.setattr(batches.workers.service, 'load_config', lambda: {'automatic_people': {'enabled': True}})
    monkeypatch.setattr(batches.database, 'connection', lambda: nullcontext(None))
    monkeypatch.setattr(batches.people, '_lock', lambda db: None)
    raw = Jobs(tmp_path, None, None, None)
    jobs = batches.ImportJobs(raw)
    parent = raw.job('import-3')
    parent.directory.mkdir()
    rows = []
    for index, (state, command) in enumerate([
        ('pending', 'pause'), ('done', 'pause'), ('excluded', 'pause'),
        ('pending', 'cancel'), ('pending', 'run'), ('pending', None),
    ], 1):
        name = f'import-3-photo-{index}'
        child = raw.job(name)
        if command:
            child.directory.mkdir()
            child.command(command)
        rows.append({'media_id': index, 'child': name, 'state': state})
    write_json(jobs.path('import-3'), {'state': 'paused', 'run_seconds': 90, 'items': rows})
    parent.command('pause')
    user = {'username': 'fixture', 'role': 'admin'}
    jobs.control('import-3', 'pause', user)
    assert json.loads(raw.job(rows[0]['child']).control.read_text())['action'] == 'pause'
    result = jobs.control('import-3', 'resume', user)
    assert result['run_seconds'] == 90
    assert json.loads(parent.control.read_text())['action'] == 'run'
    for row, expected in zip(rows, ['run', 'pause', 'pause', 'cancel', 'run', None]):
        control = raw.job(row['child']).control
        if expected:
            assert json.loads(control.read_text())['action'] == expected
        else:
            assert not control.exists()
