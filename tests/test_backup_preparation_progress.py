"""Progress is observable through all the stages before the first media copy."""
from pathlib import Path

from app.backup.admin_runner import PHASES, Run, progress_note
from app.backup.thumbnails import capture_thumbnails
from tests.test_backup_media_tree import Guard, copy, fixture_tree
from tests.test_backup_thumbnails import KEY, populate


def test_thumbnail_inventory_copy_and_readback_report_progress(tmp_path):
    data = tmp_path / 'data'
    populate(data)
    events = []
    manifest = capture_thumbnails(data, tmp_path / 'sealed', KEY,
                                  guard_factory=Guard, progress=events.append)
    phases = list(dict.fromkeys(e['phase'] for e in events))
    assert phases == ['thumbnails-inventory', 'thumbnails', 'thumbnails-verify']
    assert events[0]['entries'] == 0
    assert [e for e in events if e['phase'] == 'thumbnails-inventory'][-1]['entries'] == 4
    assert events[-1]['bytes'] == manifest['size']
    assert all(progress_note(e) for e in events)


def test_media_preparation_reports_before_first_copy(tmp_path):
    source, inventory, target = fixture_tree(tmp_path)
    events = []
    result = copy(source, inventory, target, progress=events.append)
    assert result['content_verified']
    phases = list(dict.fromkeys(e['phase'] for e in events))
    assert phases[:6] == ['copy-source-check', 'copy-paths', 'copy-inventory-check',
                          'copy-space', 'copy-folders', 'copy']
    for phase in phases[:5]:
        stage = [e for e in events if e['phase'] == phase]
        assert stage[0]['entries'] == 0
        assert stage[-1]['entries'] > 0
        assert all(progress_note(e) for e in stage)


def test_new_phases_have_browser_labels():
    script = (Path(__file__).parents[1] / 'web/backup-progress.mjs').read_text()
    for phase in ('thumbnails-inventory', 'thumbnails', 'thumbnails-verify',
                  'copy-source-check', 'copy-paths', 'copy-inventory-check', 'copy-space', 'copy-folders'):
        assert phase in PHASES
        assert f"'{phase}'" in script


def test_same_phase_stays_throttled_but_new_phase_logs_immediately(monkeypatch):
    from app.backup import admin_runner
    monkeypatch.setattr(admin_runner, 'read_run', lambda _: {'id': 'test'})
    monkeypatch.setattr(admin_runner, 'write_run', lambda _: None)
    monkeypatch.setattr(admin_runner.time, 'monotonic', lambda: 100)
    run = Run('test')
    notes = []
    monkeypatch.setattr(run, 'note', notes.append)
    run.progress({'phase': 'thumbnails', 'bytes': 0})
    run.progress({'phase': 'thumbnails', 'bytes': 100})
    run.progress({'phase': 'thumbnails-verify', 'bytes': 0})
    assert len(notes) == 2
    assert notes == ['Saving thumbnails: 0 B', 'Checking saved thumbnails: 0 B']
