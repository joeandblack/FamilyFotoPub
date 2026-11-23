from types import SimpleNamespace

import pytest

from app.backup import admin_api, admin_runner, library
from app.backup.capture_options import capture_strategy
from app.backup.diagnostics import ExplainedFailure
from tests.backup_keys import backup_key
from tests.test_backup_admin import world, api, start_body  # noqa: F401


def test_new_requests_default_to_fast_and_standard_remains_selectable():
    assert capture_strategy({}) == 'fast-full'
    assert capture_strategy({'copy_strategy': 'fast-full'}) == 'fast-full'
    assert capture_strategy({'copy_strategy': 'standard'}) == 'standard'


@pytest.mark.parametrize('old_flag', [None, False, True])
@pytest.mark.parametrize('method', [None, 'fast-full', 'standard'])
def test_start_persists_selected_method_without_opt_in(world, api, old_flag, method):  # noqa: F811
    from app.backup import admin_runs
    if old_flag is not None:
        world.config['backup']['experimental_fastbackup'] = old_flag
    payload = start_body(world)
    if method is not None:
        payload['copy_strategy'] = method
    response = api.client.post('/api/backup/start', json=payload)
    assert response.status_code == 200, response.text
    assert admin_runs.current_run()['copy_strategy'] == (method or 'fast-full')
    assert api.launched


def test_legacy_run_is_standard_and_full():
    assert capture_strategy({}, default='standard') == 'standard'


@pytest.mark.parametrize('payload', [
    {'copy_strategy': 'unknown'}, {'verification': 'unknown'}, {'verification': None},
])
def test_no_unknown_application_mode(payload):
    with pytest.raises(ExplainedFailure):
        capture_strategy(payload)


def test_api_rejects_mode_before_database_or_launch(world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(admin_api, 'connection', lambda: pytest.fail('Must reject before database access'))
    with pytest.raises(admin_api.HTTPException) as error:
        admin_api.plan_for(world.config, {'copy_strategy': 'unknown'})
    assert error.value.status_code == 422


def test_fast_runner_new_attempt_preserves_old_and_never_resumes(world, monkeypatch):  # noqa: F811
    from app.fastbackup import capture
    old = world.backup / 'old-attempt'
    old.mkdir()
    (old / 'partial').write_bytes(b'keep')
    payload = {'id': 'captured', 'media_files': 1, 'media_bytes': 5}
    value = dict(id='test-run', directory=str(old), destination=str(world.backup), copy_strategy='fast-full',
                 pg_dump=str(world.pg_dump), database_budget=1024, allow_unprotected=False)
    seen = {}
    def capture_fast(*args, **kwargs):
        seen.update(kwargs)
        seen['directory'] = args[5]
        return payload
    monkeypatch.setattr(capture, 'capture_library', capture_fast)
    monkeypatch.setattr(library, 'verify_library_metadata', lambda *args: payload)
    monkeypatch.setattr(admin_runner, 'verify_library_thumbnails', lambda *args: None)
    monkeypatch.setattr(admin_runner, 'capture_library', lambda *a, **k: pytest.fail('Wrong engine'))
    monkeypatch.setattr(admin_runner, 'verify_library', lambda *a, **k: pytest.fail('Redundant media pass'))
    run = SimpleNamespace(value=value, update=lambda **changes: value.update(changes), progress=None, note=lambda text: None)
    admin_runner.capture(run, world.config, backup_key())
    assert 'resume' not in seen
    assert seen['directory'] != old and seen['directory'].parent == world.backup
    assert value['retained_attempts'] == [str(old)]
    assert (old / 'partial').read_bytes() == b'keep'
    assert value['assurance'] == 'full' and value['verified_at']


@pytest.mark.parametrize('retained', [False, True])
def test_fast_runner_does_not_mark_failed_capture_verified(world, monkeypatch, retained):  # noqa: F811
    from app.fastbackup import capture
    value = dict(directory=str(world.backup / 'new'), copy_strategy='fast-full', pg_dump=str(world.pg_dump),
                 database_budget=1024, allow_unprotected=False)
    if retained:
        (world.backup / 'new').mkdir()
        value.update(id='retry', destination=str(world.backup), verified_at='old',
                     capture_id='old', assurance='full', media_files=1, media_bytes=5)
    def fail(*args, **kwargs):
        raise OSError('copy failed')
    monkeypatch.setattr(capture, 'capture_library', fail)
    run = SimpleNamespace(value=value, update=lambda **changes: value.update(changes), progress=None, note=lambda text: None)
    with pytest.raises(OSError):
        admin_runner.capture(run, world.config, backup_key())
    assert not value.get('verified_at')
    if retained:
        assert value['retained_attempts'] == [str(world.backup / 'new')]
        assert all(value[key] is None for key in ('capture_id', 'assurance', 'media_files', 'media_bytes'))


def test_unknown_start_rejected_without_launch(world, api, monkeypatch):  # noqa: F811
    from app.backup import admin_restore
    monkeypatch.setattr(admin_restore, 'RESTORES', world.tmp / 'restore-runs')
    monkeypatch.setattr(admin_api, 'launch_runner', lambda *a, **k: pytest.fail('Removed mode must not launch'))
    payload = start_body(world, copy_strategy='unknown', verification='unknown')
    response = api.client.post('/api/backup/start', json=payload)
    assert response.status_code == 422
    assert admin_api.restorable_backups() == []
