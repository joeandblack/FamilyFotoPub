"""The administrator's backup page: who may use it, what it checks, and how a run behaves."""
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import sys
import threading
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest

from app.auth import password_hash
from app.backup import admin_api, admin_runner, admin_runs
from tests.backup_keys import ADMIN_PASSWORD, backup_key


TOKEN = 'fixture-token'


@pytest.fixture
def world(tmp_path, monkeypatch):
    runs, config_root, data, media = (tmp_path / name for name in ('logs/backup-runs', 'config', 'data', 'media'))
    for path in (config_root, data, media, tmp_path / 'backup', tmp_path / 'keys'):
        path.mkdir(parents=True)
    pg_dump = tmp_path / 'pg_dump'
    pg_dump.write_bytes(b'fixture')
    config = {'database': {'host': '127.0.0.1', 'port': 55489, 'dbname': 'fixture', 'user': 'fixture'},
              'destination': {'root': str(media)}, 'backup': {'pg_dump': str(pg_dump)}, 'port': 4390, 'host': '127.0.0.1',
              'browser_user': 'family', 'browser_password_hash': password_hash(ADMIN_PASSWORD)}
    (config_root / 'local.json').write_text(json.dumps(config))
    monkeypatch.setattr(admin_runs, 'RUNS', runs)
    monkeypatch.setattr(admin_runs, 'PREFERENCES', runs / 'preferences.json')
    monkeypatch.setattr(admin_runs, 'DATA', data)
    monkeypatch.setattr(admin_runs, 'configuration_root', lambda: config_root)
    monkeypatch.setattr(admin_api, 'DATA', data)
    monkeypatch.setattr(admin_api, 'configuration_root', lambda: config_root)
    monkeypatch.setattr(admin_api, 'LOGS', tmp_path / 'logs')
    monkeypatch.setattr(admin_api, 'load_config', lambda: config)
    monkeypatch.setattr(admin_runner, 'DATA', data)
    monkeypatch.setattr(admin_runner, 'configuration_root', lambda: config_root)
    return SimpleNamespace(tmp=tmp_path, config=config, data=data, media=media, backup=tmp_path / 'backup',
                           password=ADMIN_PASSWORD, config_root=config_root, pg_dump=pg_dump)


def plan_of(world, destination=None, password=None, *, media_bytes=100, database_bytes=100):
    return admin_runs.check_plan(world.config, str(destination or world.backup), world.password if password is None else password,
                                 media_bytes=media_bytes, database_bytes=database_bytes)


def test_a_good_plan_has_no_problems(world):
    report = plan_of(world)
    assert report['problems'] == []
    assert report['unprotected'] is False
    assert report['needed_bytes'] == 100 + 200 + admin_runs.DUMP_ALLOWANCE


def test_plan_scans_each_state_directory_once_and_never_media(world, monkeypatch):
    from collections import Counter
    from app.backup import inventory
    scanned = []
    original = inventory.os.scandir

    def scan(path):
        scanned.append(Path(path))
        return original(path)

    monkeypatch.setattr(inventory.os, 'scandir', scan)
    assert plan_of(world)['problems'] == []
    assert Counter(scanned) == {world.config_root: 1, world.data: 1}


def test_unknown_state_does_not_hide_missing_required_settings(world):
    (world.config_root / 'local.json').unlink()
    (world.data / 'stray.bin').touch()
    problems = plan_of(world)['problems']
    assert any('Required application settings' in item for item in problems)
    assert any('data/stray.bin' in item for item in problems)


def test_plan_timing_is_returned_and_logged_without_credentials(world, api, caplog):
    with caplog.at_level(logging.INFO, logger=admin_api.__name__):
        response = api.client.post('/api/backup/plan', json=start_body(world))
    assert response.status_code == 200
    assert response.json()['check_seconds'] >= 0
    assert 'Backup plan check started' in caplog.text
    assert 'total time' in caplog.text
    assert world.password not in caplog.text


@pytest.mark.parametrize('folder, message', [
    ('missing', 'existing folder'), ('media', 'outside'), ('media/inside', 'outside'), ('data', 'outside'), ('config', 'outside')])
def test_a_bad_backup_folder_is_a_problem(world, folder, message):
    (world.media / 'inside').mkdir()
    report = plan_of(world, world.tmp / folder)
    assert any(message in problem for problem in report['problems'])


def test_a_folder_holding_the_library_is_a_problem(world):
    assert any('outside' in problem for problem in plan_of(world, world.tmp)['problems'])


def test_a_relative_folder_never_resolves_against_the_server(world):
    report = admin_runs.check_plan(world.config, '', world.password, media_bytes=1, database_bytes=1)
    assert any('existing folder' in problem for problem in report['problems'])


def test_too_little_space_is_a_problem(world, monkeypatch):
    monkeypatch.setattr(admin_runs.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10))
    def unexpected_scan(*args, **kwargs):
        pytest.fail('Insufficient space must fail before scanning application data')
    monkeypatch.setattr(admin_runs, 'state_inventory', unexpected_scan)
    assert any('enough free space' in problem for problem in plan_of(world)['problems'])


def test_rejected_start_reports_all_problems_without_launching(world, api, monkeypatch):
    monkeypatch.setattr(admin_runs.shutil, 'disk_usage', lambda path: SimpleNamespace(free=10))
    response = api.client.post('/api/backup/start', json=start_body(world, password=''))
    assert response.status_code == 409
    assert response.json()['code'] == 'backup_plan_rejected'
    problems = response.json()['problems']
    assert any('Needed about' in text and '10 bytes' in text for text in problems)
    assert any('password' in text for text in problems)
    assert not api.launched
    assert admin_runs.current_run() is None


def test_an_unprotected_disk_is_reported_not_silently_used(world, monkeypatch):
    def refuse(path):
        raise admin_runs.UnprotectedDestination('No permissions')
    monkeypatch.setattr(admin_runs, 'check_destination_privacy', refuse)
    report = plan_of(world)
    assert report['unprotected'] is True and report['problems'] == []


@pytest.mark.parametrize('password, problem', [('', 'Enter your FamilyFoto administrator password'),
                                               ('not the right password', 'not the FamilyFoto administrator password'),
                                               (ADMIN_PASSWORD + '\n', 'line breaks')])
def test_the_backup_needs_the_administrator_password(world, password, problem):
    assert any(problem in item for item in plan_of(world, password=password)['problems'])


def test_a_short_administrator_password_cannot_protect_a_backup(world):
    world.config['browser_password_hash'] = password_hash('seven77')
    assert any('too short to protect a backup' in item for item in plan_of(world, password='seven77')['problems'])


def test_eight_character_administrator_password_is_accepted(world):
    from app.backup.admin_runs import check_password
    world.config['browser_password_hash'] = password_hash('eight888')
    assert check_password(world.config, 'eight888') == []
    assert check_password(world.config, 'wrong888') == ['This is not the FamilyFoto administrator password.']


def test_another_accounts_password_is_not_the_administrators(world):
    world.config['browser_accounts'] = {'other': {'role': 'admin', 'password_hash': password_hash('other admin password')}}
    assert any('not the FamilyFoto administrator password' in item
               for item in plan_of(world, password='other admin password')['problems'])


def test_other_state_problems_are_found_before_stopping(world, monkeypatch):
    def invalid(roots, **kwargs):
        raise ValueError('Incomplete TLS certificate/key pair')
    monkeypatch.setattr(admin_runs, 'state_inventory', invalid)
    assert any('Incomplete TLS' in problem for problem in plan_of(world)['problems'])


def test_every_unknown_state_folder_is_shown_at_once(world):
    (world.data / 'old-pilot/base').mkdir(parents=True)
    for index in range(3):
        (world.data / f'old-pilot/base/{index}').write_bytes(b'x')
    (world.data / 'stray.bin').write_bytes(b'x')
    problems = plan_of(world)['problems']
    assert any('data/old-pilot' in problem and '3 files' in problem for problem in problems)
    assert any('data/stray.bin' in problem for problem in problems)


def test_a_long_list_of_unknown_state_is_shortened(world):
    for index in range(15):
        (world.data / f'stray-{index}.bin').write_bytes(b'x')
    problems = plan_of(world)['problems']
    assert len(problems) == admin_runs.UNCLASSIFIED_SHOWN + 1
    assert '5 more' in problems[-1]


def test_known_state_is_not_reported(world):
    (world.data / 'monitor.json').write_text('{}')
    (world.config_root / 'example.json').write_text('{}')
    assert plan_of(world)['problems'] == []


@pytest.mark.parametrize('kind, path, category', [
    ('data', 'postgres/base/1/1234', 'excluded'), ('data', 'postgres/PG_VERSION', 'excluded'),
    ('data', 'video-handoff.json', 'transient'), ('data', 'reports/people-integration-20261020/before.json', 'excluded'),
    ('data', f"face_detect/profiles/{'a' * 32}/desktop-server.log", 'excluded')])
def test_files_the_server_uses_or_writes_have_rules(kind, path, category):
    from app.backup.policy import classify_file
    assert classify_file(kind, path).category == category


@pytest.mark.parametrize('path', ['postgresql/x', 'postgres', 'anything.json', 'compreface/secrets/key.json', 'tls/private/x',
                                  'models/faces/x.onnx', 'backups/x.dump', 'conversion-recovery/1/interrupted-output.mp4'])
def test_leftovers_are_not_quietly_accepted(path):
    from app.backup.policy import classify_file, UnclassifiedState
    with pytest.raises(UnclassifiedState):
        classify_file('data', path)


def test_the_database_folder_passes_the_plan_check(world):
    (world.data / 'postgres/base/1').mkdir(parents=True)
    (world.data / 'postgres/base/1/1234').write_bytes(b'x')
    assert plan_of(world)['problems'] == []


def test_the_shipped_settings_template_is_not_backed_up_as_state():
    from app.backup.policy import classify_file
    assert classify_file('config', 'example.json').category == 'excluded'
    assert (Path(admin_runs.ROOT) / 'config/example.json').is_file()


def test_a_missing_pg_dump_is_a_problem(world, monkeypatch):
    world.config['backup']['pg_dump'] = str(world.tmp / 'gone')
    monkeypatch.setattr(admin_runs, 'ROOT', world.tmp)
    monkeypatch.setattr(admin_runs.shutil, 'which', lambda name: None)
    assert any('pg_dump' in problem for problem in plan_of(world)['problems'])


def record(world, **changes):
    run_id = changes.pop('id', '20261104T000000Z-00000000')
    run = {'id': run_id, 'state': 'starting', 'started_at': admin_runs.now(), 'server_pid': 1,
           'directory': str(world.backup / f'familyfoto-backup-{run_id}'), 'destination': str(world.backup),
           'allow_unprotected': False, 'pg_dump': str(world.pg_dump),
           'database_budget': 1024, 'token_sha256': admin_runs.token_digest(TOKEN),
           'restart': {'argv': ['never'], 'cwd': str(world.tmp)}, **changes}
    admin_runs.write_run(run)
    return run


def test_a_run_record_never_stores_the_token_or_the_password(api, world):
    api.client.post('/api/backup/start', json=start_body(world))
    text = ''.join(path.read_text() for path in admin_runs.RUNS.glob('*.json'))
    assert text and ADMIN_PASSWORD not in text
    assert TOKEN not in admin_runs.run_path(record(world)['id']).read_text()


def test_public_details_need_the_token(world):
    run = record(world, state='capturing', error='private path', progress={'phase': 'copy'})
    assert admin_runs.public_run(run, authorized=False) == {'authorized': False, 'state': 'capturing', 'finished': False}
    shown = admin_runs.public_run(run, authorized=True)
    assert shown['error'] == 'private path'
    assert not {'token_sha256', 'restart'} & set(shown)


def test_a_finished_run_is_not_current(world):
    record(world, state='finished')
    assert admin_runs.current_run() is None
    record(world, id='20261104T000001Z-00000001', state='capturing', runner_pid=os.getpid())
    assert admin_runs.current_run()['state'] == 'capturing'


@pytest.mark.parametrize('bad', ['../../etc', 'x', '', None, '20261104T000000Z-0000000g'])
def test_run_ids_cannot_leave_the_folder(world, bad):
    with pytest.raises(ValueError):
        admin_runs.run_path(bad)


@pytest.fixture
def api(world, monkeypatch):
    """The real routes behind a stand-in for sign-in, with no database."""
    @contextmanager
    def no_database():
        yield object()
    monkeypatch.setattr(admin_api, 'connection', no_database)
    monkeypatch.setattr(admin_api, 'sizes', lambda db: (100, 100))
    monkeypatch.setattr(admin_api, 'media_busy', lambda db: False)
    monkeypatch.setattr(admin_api, 'unclassified_database', lambda db: [])
    monkeypatch.setattr(admin_api, 'support', lambda: None)
    monkeypatch.setattr(admin_api, 'scheduled_task', lambda: None)
    monkeypatch.setattr(admin_api, 'server_module', lambda: 'app.service.desktop')
    launched = []
    monkeypatch.setattr(admin_api, 'launch_runner', lambda run_id, password: launched.append(run_id))
    role = SimpleNamespace(value='admin')
    app = FastAPI()

    @app.middleware('http')
    async def sign_in(request: Request, call_next):
        request.state.user = {'role': role.value}
        return await call_next(request)
    app.include_router(admin_api.router)
    return SimpleNamespace(client=TestClient(app, base_url='https://testserver'), role=role, launched=launched)


def start_body(world, **changes):
    return {'destination': str(world.backup), 'password': world.password, 'confirm_stop': True, **changes}


@pytest.mark.parametrize('method, path', [
    ('get', '/api/backup'), ('post', '/api/backup/plan'), ('post', '/api/backup/start'),
    ('get', '/api/backup-run'), ('get', '/backup-progress')])
@pytest.mark.parametrize('role', ['regular', 'guest'])
def test_only_an_administrator_can_use_it(api, method, path, role):
    api.role.value = role
    response = getattr(api.client, method)(path, **({'json': {}} if method == 'post' else {}))
    assert response.status_code == 403


def test_overview_reports_sizes_and_never_tokens(api, world):
    record(world, state='finished', verified_at=admin_runs.now())
    body = api.client.get('/api/backup').json()
    assert body['media_bytes'] == 100 and body['runs'][0]['verified_at']
    assert 'token_sha256' not in body['runs'][0] and 'key_file' not in body['runs'][0]


def test_start_needs_the_stop_confirmed(api, world):
    response = api.client.post('/api/backup/start', json=start_body(world, confirm_stop=False))
    assert response.status_code == 422 and not api.launched


def test_start_refuses_a_bad_plan(api, world):
    response = api.client.post('/api/backup/start', json=start_body(world, password='not the right password'))
    assert response.status_code == 409 and not api.launched
    assert 'not the right password' not in response.text


def test_start_refuses_when_media_work_is_running(api, world, monkeypatch):
    monkeypatch.setattr(admin_api, 'media_busy', lambda db: True)
    response = api.client.post('/api/backup/start', json=start_body(world))
    assert response.status_code == 409 and 'running' in response.json()['detail'] and not api.launched


def test_start_refuses_on_an_unsupported_server(api, world, monkeypatch):
    monkeypatch.setattr(admin_api, 'support', lambda: 'Use the backup command.')
    assert api.client.post('/api/backup/start', json=start_body(world)).status_code == 409
    assert not api.launched


def test_start_refuses_a_second_run(api, world, monkeypatch):
    monkeypatch.setattr(admin_runs, 'process_alive', lambda pid: True)
    record(world, state='capturing', runner_pid=4242)
    assert api.client.post('/api/backup/start', json=start_body(world)).status_code == 409
    assert not api.launched


def test_a_failed_run_blocks_another_until_it_is_decided(api, world, monkeypatch):
    monkeypatch.setattr(admin_runs, 'process_alive', lambda pid: True)
    record(world, state='failed', runner_pid=4242)
    assert api.client.post('/api/backup/start', json=start_body(world)).status_code == 409


def test_two_starts_at_once_launch_one_runner(api, world, monkeypatch):
    real = admin_api.plan_for
    both_inside, results = threading.Barrier(2, timeout=5), []

    def slow(config, payload):
        try:
            both_inside.wait()
        except threading.BrokenBarrierError:
            pass
        return real(config, payload)
    monkeypatch.setattr(admin_api, 'plan_for', slow)

    def post():
        results.append(TestClient(api.client.app, base_url='https://testserver').post(
            '/api/backup/start', json=start_body(world)).status_code)
    threads = [threading.Thread(target=post) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert sorted(results) == [200, 409]
    assert len(api.launched) == 1 and len(admin_runs.all_runs()) == 1


def test_a_failed_start_lets_the_next_one_try(api, world):
    assert api.client.post('/api/backup/start', json=start_body(world, password='')).status_code == 409
    assert api.client.post('/api/backup/start', json=start_body(world)).status_code == 200


def test_a_record_whose_runner_has_gone_does_not_block(api, world, monkeypatch):
    monkeypatch.setattr(admin_runs, 'process_alive', lambda pid: False)
    run = record(world, state='failed', runner_pid=4242, error='Disk disconnected')
    assert api.client.post('/api/backup/start', json=start_body(world)).status_code == 200
    saved = admin_runs.read_run(run['id'])
    assert saved['state'] == 'finished' and saved['gave_up'] is True and saved['error'] == 'Disk disconnected'


def test_a_new_run_gets_time_to_report_its_runner(world):
    run = record(world, state='starting')
    assert admin_runs.current_run()['id'] == run['id']


def test_a_run_that_never_reported_a_runner_ends(world, monkeypatch):
    run = record(world, state='starting', started_at='2026-01-01T00:00:00+00:00')
    assert admin_runs.current_run() is None
    assert admin_runs.read_run(run['id'])['state'] == 'finished'


def test_a_live_runner_keeps_its_run_current(world, monkeypatch):
    monkeypatch.setattr(admin_runs, 'process_alive', lambda pid: pid == 4242)
    run = record(world, state='capturing', runner_pid=4242)
    assert admin_runs.current_run()['id'] == run['id']


def test_an_unprotected_disk_needs_explicit_acceptance(api, world, monkeypatch):
    def refuse(path):
        raise admin_runs.UnprotectedDestination('No permissions')
    monkeypatch.setattr(admin_runs, 'check_destination_privacy', refuse)
    response = api.client.post('/api/backup/start', json=start_body(world))
    assert response.status_code == 409 and 'private' in response.json()['detail'] and not api.launched
    assert api.client.post('/api/backup/start', json=start_body(world, allow_unprotected=True)).status_code == 200
    assert admin_runs.read_run(api.launched[0])['allow_unprotected'] is True


@pytest.mark.parametrize('value', ['true', 1, 'yes'])
def test_acceptance_must_be_exactly_true(api, world, monkeypatch, value):
    def refuse(path):
        raise admin_runs.UnprotectedDestination('No permissions')
    monkeypatch.setattr(admin_runs, 'check_destination_privacy', refuse)
    assert api.client.post('/api/backup/start', json=start_body(world, allow_unprotected=value)).status_code == 409


def test_start_records_a_run_and_gives_only_this_browser_the_token(api, world):
    response = api.client.post('/api/backup/start', json=start_body(world))
    assert response.status_code == 200
    run = admin_runs.read_run(api.launched[0])
    assert run['state'] == 'starting' and run['server_pid'] == os.getpid()
    assert Path(run['directory']).parent == world.backup and not Path(run['directory']).exists()
    cookie = response.headers['set-cookie']
    assert 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=strict' in cookie
    token = response.cookies[admin_runs.COOKIE]
    assert admin_runs.token_digest(token) == run['token_sha256'] and token not in response.text


def test_status_shows_details_only_to_the_starting_browser(api, world):
    api.client.post('/api/backup/start', json=start_body(world))
    assert api.client.get('/api/backup-run').json()['authorized'] is True
    api.client.cookies.clear()
    assert api.client.get('/api/backup-run').json()['authorized'] is False


def test_preferences_are_remembered_without_the_password(api, world):
    admin_runs.RUNS.mkdir(parents=True, exist_ok=True)
    admin_runs.PREFERENCES.write_text(json.dumps({'destination': 'old', 'key_file': 'C:/old/recovery.key'}))
    api.client.post('/api/backup/start', json=start_body(world))
    preferences = api.client.get('/api/backup').json()['preferences']
    assert preferences == {'destination': str(world.backup)}
    text = admin_runs.PREFERENCES.read_text()
    assert ADMIN_PASSWORD not in text and 'key_file' not in text  # An old key path is dropped, never read.


def test_a_test_copy_never_restarts_the_real_task(monkeypatch):
    """The installed task runs another copy's script, so this one must start itself."""
    monkeypatch.setattr(admin_api, 'ROOT', Path(r'D:\other-copy'))
    monkeypatch.setattr(admin_api, 'server_task', lambda: admin_api.TASK)
    monkeypatch.setattr(admin_api.sys, 'platform', 'win32')
    output = SimpleNamespace(returncode=0, stdout=r'-File "D:\PROJECTS\real\scripts\desktop-server.ps1"')
    monkeypatch.setattr(admin_api.subprocess, 'run', lambda *args, **kwargs: output)
    monkeypatch.setattr(admin_api.subprocess, 'CREATE_NO_WINDOW', 0, raising=False)
    assert admin_api.scheduled_task() is None


def test_the_real_copy_restarts_its_own_task(monkeypatch):
    monkeypatch.setattr(admin_api, 'ROOT', Path(r'D:\PROJECTS\real'))
    monkeypatch.setattr(admin_api, 'server_task', lambda: admin_api.TASK)
    monkeypatch.setattr(admin_api.sys, 'platform', 'win32')
    output = SimpleNamespace(returncode=0, stdout=r'-File "D:\PROJECTS\real\scripts\desktop-server.ps1"')
    monkeypatch.setattr(admin_api.subprocess, 'run', lambda *args, **kwargs: output)
    monkeypatch.setattr(admin_api.subprocess, 'CREATE_NO_WINDOW', 0, raising=False)
    assert admin_api.scheduled_task() == admin_api.TASK


def test_a_named_installation_restarts_its_own_task(monkeypatch):
    """A second installation on the computer must never start the first one's task."""
    monkeypatch.setattr(admin_api, 'ROOT', Path(r'D:\PROJECTS\second'))
    monkeypatch.setattr(admin_api, 'load_config', lambda: {'instance': 'restore-test'})
    monkeypatch.setattr(admin_api.sys, 'platform', 'win32')
    asked = []

    def run(argv, **kwargs):
        asked.append(argv[-1])
        return SimpleNamespace(returncode=0, stdout=r'-File "D:\PROJECTS\second\scripts\desktop-server.ps1"')
    monkeypatch.setattr(admin_api.subprocess, 'run', run)
    monkeypatch.setattr(admin_api.subprocess, 'CREATE_NO_WINDOW', 0, raising=False)
    assert admin_api.scheduled_task() == 'FamilyFoto Desktop Server (restore-test)'
    assert "'FamilyFoto Desktop Server (restore-test)'" in asked[0]


def test_only_servers_with_a_stop_signal_are_supported(monkeypatch):
    monkeypatch.setattr(admin_api.sys, 'platform', 'win32')
    monkeypatch.setattr(admin_api, 'server_module', lambda: 'uvicorn')
    assert admin_api.support()
    monkeypatch.setattr(admin_api, 'server_module', lambda: 'app.service.live')
    assert admin_api.support() is None
    monkeypatch.setattr(admin_api, 'server_module', lambda: 'app.migration.launch')
    assert admin_api.support() is None


def test_a_recovered_server_comes_back_as_itself_not_as_the_scheduled_task(monkeypatch):
    monkeypatch.setattr(admin_api, 'server_module', lambda: 'app.migration.launch')
    monkeypatch.setattr(admin_api, 'scheduled_task', lambda: pytest.fail('The task starts the old settings'))
    monkeypatch.setattr(admin_api, 'DATA', Path('recovery/catalog/files/data'))
    plan = admin_api.restart_plan()
    assert plan['argv'][1:] == ['-m', 'app.migration.launch', '--directory', str(Path('recovery'))]


def status_client(world, **changes):
    run = admin_runner.Run(record(world, **changes)['id'])
    client = TestClient(admin_runner.status_app(run), base_url='https://testserver')
    return run, client


def test_the_progress_page_is_open_but_details_are_not(world):
    _, client = status_client(world, state='capturing', error='private path')
    assert 'FamilyFoto backup' in client.get('/').text
    assert client.get('/api/backup-run').json() == {'authorized': False, 'state': 'capturing', 'finished': False}
    client.cookies.set(admin_runs.COOKIE, TOKEN)
    assert client.get('/api/backup-run').json()['error'] == 'private path'


def test_a_wrong_token_gets_nothing(world):
    run, client = status_client(world, state='failed')
    client.cookies.set(admin_runs.COOKIE, 'wrong')
    assert client.get('/api/backup-run').json()['authorized'] is False
    assert client.post('/api/backup-run/decision', json={'action': 'retry'}).status_code == 403
    assert not run.decision.is_set()


@pytest.mark.parametrize('method,path', [('get', '/api/status'), ('get', '/api/face-service'),
                                          ('get', '/api/backup/restores'), ('post', '/api/auth/activity')])
def test_offline_app_endpoints_return_json_not_the_progress_html(world, method, path):
    _, client = status_client(world, state='capturing')
    response = getattr(client, method)(path)
    assert response.status_code == 503
    assert response.json() == {'error': 'FamilyFoto is stopped while the backup runs.'}
    assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('previous_handler', [False, True])
def test_progress_socket_reset_is_reported_once_other_errors_still_reach_handler(world, previous_handler):
    import asyncio
    run = admin_runner.Run(record(world)['id'])
    app = admin_runner.status_app(run)

    async def check():
        loop = asyncio.get_running_loop()
        old = loop.get_exception_handler()
        forwarded = []
        previous = lambda loop, context: forwarded.append(context)
        default = loop.default_exception_handler
        loop.default_exception_handler = forwarded.append
        loop.set_exception_handler(previous if previous_handler else None)
        callback = lambda: None
        callback.__module__ = 'asyncio.proactor_events'
        callback.__qualname__ = '_ProactorBasePipeTransport._call_connection_lost'
        reset = ConnectionResetError(10054, 'client reset')
        context = {'exception': reset, 'handle': SimpleNamespace(_callback=callback)}
        try:
            async with app.router.lifespan_context(app):
                handler = loop.get_exception_handler()
                handler(loop, context)
                handler(loop, context)
                assert not forwarded
                for error in [OSError(22, 'disk error'), ConnectionResetError(123, 'different reset')]:
                    other = {**context, 'exception': error}
                    handler(loop, other)
                    assert forwarded[-1] is other
                other = {'exception': reset, 'handle': SimpleNamespace(_callback=lambda: None)}
                handler(loop, other)
                assert forwarded[-1] is other
            assert loop.get_exception_handler() == (previous if previous_handler else None)
        finally:
            loop.set_exception_handler(old)
            loop.default_exception_handler = default

    asyncio.run(check())
    text = admin_runs.read_activity(run.value['id'], 0)['text']
    assert text.count('connection was reset by its client') == 1
    assert 'Backup work continues independently' in text


def test_a_decision_needs_the_token(world):
    run, client = status_client(world, state='failed')
    assert client.post('/api/backup-run/decision', json={'action': 'restart'}).status_code == 403
    client.cookies.set(admin_runs.COOKIE, TOKEN)
    assert client.post('/api/backup-run/decision', json={'action': 'restart'}).status_code == 200
    assert run.decision.is_set() and run.action == 'restart'


def test_a_decision_from_another_site_is_refused(world):
    run, client = status_client(world, state='failed')
    client.cookies.set(admin_runs.COOKIE, TOKEN)
    response = client.post('/api/backup-run/decision', json={'action': 'retry'}, headers={'origin': 'https://elsewhere'})
    assert response.status_code == 403 and not run.decision.is_set()


def test_the_log_is_read_in_whole_lines_from_where_the_page_left_off(world, monkeypatch):
    run_id = record(world)['id']
    assert admin_runs.read_activity(run_id, 0) == {'text': '', 'next': 0}
    admin_runs.note_activity(run_id, 'first\nsecond')
    first = admin_runs.read_activity(run_id, 0)
    assert first['text'].splitlines()[0].endswith('  first') and first['text'].count('\n') == 2
    admin_runs.note_activity(run_id, 'third')
    after = admin_runs.read_activity(run_id, first['next'])
    assert after['text'].strip().endswith('third') and 'first' not in after['text']
    monkeypatch.setattr(admin_runs, 'LOG_READ_BYTES', len(first['text'].encode()) - 3)
    assert admin_runs.read_activity(run_id, 0)['text'].count('\n') == 1  # Never half a line.
    for bad in (-1, '0', 1.5):
        with pytest.raises(ValueError):
            admin_runs.read_activity(run_id, bad)


def test_a_line_being_written_waits_for_the_next_read(world):
    """Half a line could end in half a character, such as one in a Chinese folder name."""
    run_id = record(world)['id']
    admin_runs.note_activity(run_id, 'Backing up into F:\\备份')
    whole = admin_runs.activity_path(run_id).read_bytes()
    with admin_runs.activity_path(run_id).open('ab') as output:
        output.write('Copying 照片'.encode('utf-8')[:-1])  # Mid-character, as a reader might find it.
    read = admin_runs.read_activity(run_id, 0)
    assert read == {'text': whole.decode('utf-8'), 'next': len(whole)} and '\ufffd' not in read['text']
    assert admin_runs.read_activity(run_id, read['next']) == {'text': '', 'next': len(whole)}


def test_a_log_write_that_fails_never_stops_the_start(api, world, monkeypatch):
    def full(*args):
        raise OSError(28, 'No space left on device')
    monkeypatch.setattr(admin_runs, 'note_activity', full)
    assert api.client.post('/api/backup/start', json=start_body(world)).status_code == 200
    assert api.launched


def test_the_run_log_takes_warnings_not_everyday_messages(world):
    import logging
    run_id = record(world)['id']
    handler = admin_runner.ActivityHandler(run_id)
    logger = logging.getLogger('test-backup-log')
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info('connection pool opened')
        logger.warning('disk is nearly full')
    finally:
        logger.removeHandler(handler)
    text = admin_runs.read_activity(run_id, 0)['text']
    assert 'warning: disk is nearly full' in text and 'pool opened' not in text


def test_unexpected_logged_exception_keeps_traceback(world):
    run_id = record(world)['id']
    try:
        raise OSError(22, 'fixture invalid argument')
    except OSError:
        record_value = logging.LogRecord('asyncio', logging.ERROR, __file__, 0,
                                        'Unexpected callback failure', (), sys.exc_info())
    admin_runner.ActivityHandler(run_id).emit(record_value)
    text = admin_runs.read_activity(run_id, 0)['text']
    assert 'Unexpected callback failure' in text
    assert 'Traceback' in text and 'fixture invalid argument' in text


def test_the_run_writes_its_steps_into_the_log_in_words(world, monkeypatch):
    monkeypatch.setattr(admin_runner, 'LOG_PROGRESS_SECONDS', 3600)
    run = admin_runner.Run(record(world)['id'])
    run.update(state='capturing')
    run.update(state='capturing')  # The same state again says nothing new.
    for files in range(3):
        run.progress({'phase': 'copy', 'files': files, 'total_files': 10, 'bytes': files * 1024, 'total_bytes': 10240})
    run.progress({'phase': 'verify', 'files': 1, 'total_files': 10, 'bytes': 1024, 'total_bytes': 10240})
    run.update(state='failed', error='The disk was removed')
    lines = admin_runs.read_activity(run.value['id'], 0)['text'].splitlines()
    assert [line.split('  ', 1)[1] for line in lines] == [
        admin_runner.STATE_NOTES['capturing'],
        'Copying photos and videos: 0 of 10 files, 0 B of 10.0 KB',
        'Checking the copies: 1 of 10 files, 1.0 KB of 10.0 KB',
        admin_runner.STATE_NOTES['failed'],
        'Error: The disk was removed']


@pytest.mark.parametrize('result', [{}, {'gave_up': True}])
def test_finished_run_logs_total_once_after_finish(world, result):
    value = record(world, started_at='2026-11-19T22:35:29.615075-08:00')
    run = admin_runner.Run(value['id'])
    run.update(state='finished', finished_at='2026-11-20T08:25:30.849209+00:00', **result)
    run.update(state='finished')
    lines = admin_runs.read_activity(value['id'], 0)['text'].splitlines()
    assert 'Finished.' in lines[-2]
    assert lines[-1].endswith('Total elapsed time: 1h 50m 1s (start to finish, including waits and restart).')
    assert sum('Total elapsed time:' in line for line in lines) == 1


@pytest.mark.parametrize('changes', [
    {'started_at': None}, {'finished_at': 'bad'}, {'finished_at': None},
    {'finished_at': '2026-11-19T00:00:00+00:00'}, {'started_at': '2026-11-20T00:00:00'},
])
def test_invalid_total_time_is_not_invented(changes):
    value = dict(started_at='2026-11-20T00:00:00+00:00', finished_at='2026-11-20T01:00:00+00:00')
    assert admin_runner.total_time_note({**value, **changes}) is None


def test_a_long_step_reports_its_progress_now_and_then(world, monkeypatch):
    monkeypatch.setattr(admin_runner, 'LOG_PROGRESS_SECONDS', 0)
    run = admin_runner.Run(record(world)['id'])
    for files in range(3):
        run.progress({'phase': 'copy', 'files': files, 'total_files': 10, 'bytes': 0, 'total_bytes': 10})
    assert admin_runs.read_activity(run.value['id'], 0)['text'].count('Copying') == 3


def test_the_progress_page_log_needs_the_token(world):
    run, client = status_client(world, state='capturing')
    run.note('Backing up')
    assert client.get('/api/backup-run/log').status_code == 403
    client.cookies.set(admin_runs.COOKIE, TOKEN)
    response = client.get('/api/backup-run/log')
    assert response.status_code == 200 and 'Backing up' in response.json()['text']
    assert client.get('/api/backup-run/log', params={'after': -1}).status_code == 422


def test_log_errors_do_not_expose_exception_details(world, monkeypatch):
    run, client = status_client(world, state='capturing')
    client.cookies.set(admin_runs.COOKIE, TOKEN)
    def fail(*args):
        raise ValueError('internal path or sensitive diagnostic')
    monkeypatch.setattr(admin_runner, 'read_activity', fail)
    response = client.get('/api/backup-run/log')
    assert response.status_code == 422
    assert response.json() == {'error': 'Invalid log position'}


def test_once_familyfoto_is_back_it_serves_the_log_to_the_starting_browser(api, world):
    api.client.post('/api/backup/start', json=start_body(world))
    response = api.client.get('/api/backup-run/log')
    assert response.status_code == 200 and 'Backup started from Settings' in response.json()['text']
    api.client.cookies.clear()
    assert api.client.get('/api/backup-run/log').status_code == 403


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows process creation flags')
def test_background_processes_open_no_console_window():
    import subprocess
    flags = admin_runs.detached_options()['creationflags']
    assert flags & subprocess.CREATE_NO_WINDOW and not flags & subprocess.DETACHED_PROCESS
    assert flags & subprocess.CREATE_BREAKAWAY_FROM_JOB


@pytest.mark.parametrize('state', ['capturing', 'verifying', 'restarting'])
def test_no_decision_while_it_is_not_waiting_for_one(world, state):
    run, client = status_client(world, state=state)
    client.cookies.set(admin_runs.COOKIE, TOKEN)
    assert client.post('/api/backup-run/decision', json={'action': 'retry'}).status_code == 409
    assert not run.decision.is_set()


def test_an_unknown_decision_is_refused(world):
    run, client = status_client(world, state='failed')
    client.cookies.set(admin_runs.COOKIE, TOKEN)
    assert client.post('/api/backup-run/decision', json={'action': 'delete'}).status_code == 409


@pytest.mark.parametrize('name', ['app.js', 'index.html'])
def test_the_progress_server_only_serves_its_own_files(world, name):
    _, client = status_client(world, state='capturing')
    assert client.get(f'/web/{name}').status_code == 404


def test_the_progress_server_cannot_be_walked_out_of_its_folder(world):
    _, client = status_client(world, state='capturing')
    for path in ('/web/..%2f..%2fconfig%2flocal.json', '/web/../../config/local.json', '/config/local.json'):
        assert 'database' not in client.get(path).text


def test_a_status_read_cannot_end_a_backup(world, monkeypatch):
    run = record(world)
    real, attempts = admin_runs.save_private, []

    def busy_once(path, value):
        attempts.append(path)
        if len(attempts) == 1:
            raise PermissionError('Another process is reading this file')
        return real(path, value)
    monkeypatch.setattr(admin_runs, 'save_private', busy_once)
    monkeypatch.setattr(admin_runs.time, 'sleep', lambda seconds: None)
    admin_runs.write_run({**run, 'state': 'capturing'})
    assert len(attempts) == 2 and admin_runs.read_run(run['id'])['state'] == 'capturing'


def test_a_record_that_stays_unwritable_is_still_reported(world, monkeypatch):
    run = record(world)

    def always_busy(path, value):
        raise PermissionError('Refused')
    monkeypatch.setattr(admin_runs, 'save_private', always_busy)
    monkeypatch.setattr(admin_runs.time, 'sleep', lambda seconds: None)
    with pytest.raises(PermissionError):
        admin_runs.write_run(run)


def test_the_runner_never_takes_an_application_lease():
    source = Path(admin_runner.__file__).read_text(encoding='utf-8')
    code = '\n'.join(line for line in source.splitlines() if not line.lstrip().startswith('#'))
    assert 'load_config' not in code.replace('Not load_config', '').replace('application\'s cached', '')
    assert 'hold_process_lease' not in code and 'initialize_runtime' not in code


class Flow:
    """A run with everything around the capture recorded instead of done."""

    def __init__(self, world, monkeypatch, **changes):
        self.calls = []
        self.outcomes = []
        self.run_id = record(world, **changes)['id']
        monkeypatch.setattr(admin_runner, 'stop_application', self.stop)
        monkeypatch.setattr(admin_runner, 'capture', self.capture)
        monkeypatch.setattr(admin_runner, 'acknowledge_gate', lambda: self.calls.append('acknowledge'))
        monkeypatch.setattr(admin_runner, 'restart_application', lambda run: self.calls.append('restart'))
        monkeypatch.setattr(admin_runner, 'StatusServer', self.server)
        monkeypatch.setattr(admin_runner.time, 'sleep', lambda seconds: None)

    def until_waiting(self):
        """Until the run is showing its page and has reported a failure."""
        pause = threading.Event()
        for _ in range(1000):
            if 'page' in self.calls and self.state()['state'] == 'failed':
                return True
            pause.wait(0.01)
        return False

    def server(self, run, config):
        self.run = run
        self.calls.append('page')
        return SimpleNamespace(start=lambda: None, stop=lambda: self.calls.append('page-stopped'))

    def stop(self, run):
        self.run = run
        self.calls.append('stop')

    def capture(self, run, config, key=None):
        self.calls.append('capture')
        if self.outcomes.pop(0) == 'fail':
            raise OSError('Disk disconnected')
        run.update(verified_at=admin_runs.now(), state='verifying')

    def start(self):
        thread = threading.Thread(target=admin_runner.run_backup, args=(self.run_id,), daemon=True)
        thread.start()
        return thread

    def state(self):
        return admin_runs.read_run(self.run_id)


def test_a_good_run_stops_captures_and_restarts_in_that_order(world, monkeypatch):
    flow = Flow(world, monkeypatch)
    flow.outcomes = ['ok']
    flow.start().join(10)
    assert flow.calls == ['stop', 'page', 'capture', 'page-stopped', 'restart']
    assert flow.state()['state'] == 'finished' and flow.state()['verified_at']


def test_a_failed_run_waits_and_restarts_nothing(world, monkeypatch):
    flow = Flow(world, monkeypatch)
    flow.outcomes = ['fail']
    thread = flow.start()
    assert flow.until_waiting()
    assert 'restart' not in flow.calls and 'acknowledge' not in flow.calls
    assert flow.state()['state'] == 'failed' and 'disconnected' in flow.state()['error']
    assert thread.is_alive()
    flow.run.action = 'restart'
    flow.run.decision.set()
    thread.join(10)
    assert flow.calls[-3:] == ['acknowledge', 'page-stopped', 'restart']
    assert flow.state()['gave_up'] is True and 'verified_at' not in flow.state()


def test_a_retry_acknowledges_only_on_request_then_captures_again(world, monkeypatch):
    flow = Flow(world, monkeypatch)
    flow.outcomes = ['fail', 'ok']
    thread = flow.start()
    assert flow.until_waiting()
    flow.run.action = 'retry'
    flow.run.decision.set()
    thread.join(10)
    assert flow.calls == ['stop', 'page', 'capture', 'acknowledge', 'capture', 'page-stopped', 'restart']
    assert flow.state()['verified_at'] and not flow.state().get('gave_up')


def test_failing_to_stop_the_application_ends_the_run_without_a_gate(world, monkeypatch):
    flow = Flow(world, monkeypatch)

    def cannot_stop(run):
        raise RuntimeError('The application did not stop in time')
    monkeypatch.setattr(admin_runner, 'stop_application', cannot_stop)
    flow.start().join(10)
    assert flow.calls == []
    assert flow.state()['state'] == 'finished' and flow.state()['gave_up'] is True


def test_a_run_found_mid_way_asks_instead_of_carrying_on(world, monkeypatch):
    flow = Flow(world, monkeypatch, state='capturing')
    thread = flow.start()
    assert flow.until_waiting()
    assert flow.calls == ['page'] and flow.state()['state'] == 'failed'
    flow.run.action = 'restart'
    flow.run.decision.set()
    thread.join(10)


def test_failing_to_restart_is_reported(world, monkeypatch):
    flow = Flow(world, monkeypatch)
    flow.outcomes = ['ok']

    def cannot(run):
        raise OSError('No task')
    monkeypatch.setattr(admin_runner, 'restart_application', cannot)
    flow.start().join(10)
    assert 'could not be restarted' in flow.state()['restart_error']
    assert flow.state()['state'] == 'finished'


def test_stopping_signals_only_this_data_folder_and_waits(world, monkeypatch):
    run = admin_runner.Run(record(world, server_pid=4242)['id'])
    alive = iter([True, True, False])
    seen = []

    def still_running(pid):
        seen.append((pid, (world.data / 'DESKTOP-SERVER-STOP').exists()))
        return next(alive)
    monkeypatch.setattr(admin_runner, 'process_alive', still_running)
    monkeypatch.setattr(admin_runner.time, 'sleep', lambda seconds: None)
    admin_runner.stop_application(run)
    assert seen == [(4242, True)] * 3
    assert not (world.data / 'DESKTOP-SERVER-STOP').exists()


def test_a_server_that_will_not_stop_gets_its_stop_signal_removed(world, monkeypatch):
    run = admin_runner.Run(record(world, server_pid=4242)['id'])
    monkeypatch.setattr(admin_runner, 'process_alive', lambda pid: True)
    monkeypatch.setattr(admin_runner, 'STOP_TIMEOUT', -1)
    with pytest.raises(RuntimeError, match='did not stop'):
        admin_runner.stop_application(run)
    assert not (world.data / 'DESKTOP-SERVER-STOP').exists()


def test_the_progress_page_really_starts(world, monkeypatch):
    monkeypatch.setenv('FAMILYFOTO_DEV_HTTP', '1')
    run = admin_runner.Run(record(world, state='capturing')['id'])
    page = admin_runner.StatusServer(run, {**world.config, 'port': 0})
    page.start()
    try:
        assert page.server.started is True
    finally:
        page.stop()


def test_a_page_that_cannot_start_is_reported(world, monkeypatch):
    monkeypatch.setenv('FAMILYFOTO_DEV_HTTP', '1')
    run = admin_runner.Run(record(world, state='capturing')['id'])
    page = admin_runner.StatusServer(run, {**world.config, 'port': 0})
    monkeypatch.setattr(page.server, 'run', lambda: None)
    page.thread = threading.Thread(target=lambda: None, daemon=True)
    with pytest.raises(RuntimeError, match='could not start'):
        page.start()


def test_a_crash_with_no_gate_brings_the_application_back(world, monkeypatch):
    run = record(world, state='stopping-app')
    (world.data / 'DESKTOP-SERVER-STOP').write_text('x')
    restarted = []
    monkeypatch.setattr(admin_runner, 'restart_application', lambda value: restarted.append(value))

    def crash(run_id, key=None):
        raise TypeError('Bug in the runner')
    monkeypatch.setattr(admin_runner, 'run_backup', crash)
    with pytest.raises(TypeError):
        admin_runner.main(['--run-id', run['id']])
    assert restarted and not (world.data / 'DESKTOP-SERVER-STOP').exists()
    saved = admin_runs.read_run(run['id'])
    assert saved['state'] == 'finished' and saved['gave_up'] is True and 'Bug' in saved['error']


def test_a_crash_with_a_gate_leaves_it_for_a_decision(world, monkeypatch):
    run = record(world, state='capturing')
    admission = world.data / '.recovery-admission'
    admission.mkdir()
    (admission / 'maintenance.json').write_text('{}')
    monkeypatch.setattr(admin_runner, 'restart_application', lambda value: pytest.fail('Must not restart behind a gate'))

    def crash(run_id, key=None):
        raise TypeError('Bug in the runner')
    monkeypatch.setattr(admin_runner, 'run_backup', crash)
    with pytest.raises(TypeError):
        admin_runner.main(['--run-id', run['id']])
    assert admin_runs.read_run(run['id'])['state'] == 'failed'
    assert (admission / 'maintenance.json').exists()


def test_a_crash_that_cannot_restart_says_so(world, monkeypatch):
    run = record(world, state='stopping-app')

    def cannot(value):
        raise OSError('No task')
    monkeypatch.setattr(admin_runner, 'restart_application', cannot)

    def crash(run_id, key=None):
        raise TypeError('Bug in the runner')
    monkeypatch.setattr(admin_runner, 'run_backup', crash)
    with pytest.raises(TypeError):
        admin_runner.main(['--run-id', run['id']])
    assert 'could not be restarted' in admin_runs.read_run(run['id'])['restart_error']


def test_this_process_counts_as_alive():
    assert admin_runs.process_alive(os.getpid()) is True


def test_a_failed_check_is_retried_by_checking_again(world, monkeypatch):
    """Capturing again would be refused, so a sealed backup must only be verified again."""
    directory = world.backup / 'familyfoto-backup-20261104T000000Z-00000000'
    (directory / 'library').mkdir(parents=True)
    (directory / 'library/complete.fernet').write_bytes(b'sealed')
    run = admin_runner.Run(record(world, state='failed')['id'])
    monkeypatch.setattr(admin_runner, 'capture_library', lambda *a, **k: pytest.fail('Must not capture again'))
    monkeypatch.setattr(admin_runner, 'verify_library', lambda d, key, progress: {'id': 'c' * 32, 'media_files': 3, 'media_bytes': 9})
    admin_runner.capture(run, world.config, backup_key())
    saved = admin_runs.read_run(run.value['id'])
    assert saved['verified_at'] and saved['media_files'] == 3 and saved['error'] is None


def test_a_check_that_fails_again_reports_it_and_claims_nothing(world, monkeypatch):
    directory = world.backup / 'familyfoto-backup-20261104T000000Z-00000000'
    (directory / 'library').mkdir(parents=True)
    (directory / 'library/complete.fernet').write_bytes(b'sealed')
    run = admin_runner.Run(record(world, state='failed')['id'])

    def damaged(directory, key, progress):
        raise ValueError('Copied media failed content verification')
    monkeypatch.setattr(admin_runner, 'verify_library', damaged)
    with pytest.raises(ValueError, match='verification'):
        admin_runner.capture(run, world.config, backup_key())
    assert 'verified_at' not in admin_runs.read_run(run.value['id'])


def test_a_token_is_compared_in_constant_time(world, monkeypatch):
    seen = []
    real = admin_runs.hmac.compare_digest

    def spy(left, right):
        seen.append(True)
        return real(left, right)
    monkeypatch.setattr(admin_runs.hmac, 'compare_digest', spy)
    run = record(world)
    assert admin_runs.token_matches(TOKEN, run) is True
    assert admin_runs.token_matches('wrong', run) is False
    assert admin_runs.token_matches('', run) is False
    assert len(seen) == 2


def test_only_a_backup_gate_can_be_acknowledged(world):
    admission = world.data / '.recovery-admission'
    admission.mkdir()
    (admission / 'maintenance.json').write_text(json.dumps({'id': 'a' * 32, 'operation': 'upgrade', 'state': 'in-progress'}))
    with pytest.raises(RuntimeError, match='Another kind'):
        admin_runner.acknowledge_gate()
    assert (admission / 'maintenance.json').exists()


def test_a_backup_gate_is_acknowledged(world):
    admission = world.data / '.recovery-admission'
    admission.mkdir()
    (admission / 'maintenance.json').write_text(json.dumps({'id': 'a' * 32, 'operation': 'backup', 'state': 'in-progress'}))
    admin_runner.acknowledge_gate()
    assert not (admission / 'maintenance.json').exists()


def test_no_gate_means_nothing_to_acknowledge(world):
    admin_runner.acknowledge_gate()


def test_progress_writes_are_throttled(world):
    run = admin_runner.Run(record(world)['id'])
    run.progress({'phase': 'copy', 'files': 1})
    run.progress({'phase': 'copy', 'files': 2})
    assert run.value['progress']['files'] == 2
    assert admin_runs.read_run(run.value['id'])['progress']['files'] == 1


def test_the_retirement_flow_is_not_offered():
    for module in (admin_api, admin_runner):
        assert 'retire_to' not in Path(module.__file__).read_text(encoding='utf-8')


def test_a_mac_server_started_by_the_service_can_back_up(monkeypatch):
    monkeypatch.setattr(admin_api.sys, 'platform', 'darwin')
    monkeypatch.setattr(admin_api, 'server_module', lambda: 'app.service.macos')
    assert admin_api.support() is None
    for other in ('uvicorn', 'app.service.live', 'app.migration.launch', None):
        monkeypatch.setattr(admin_api, 'server_module', lambda other=other: other)
        assert 'backup command' in admin_api.support()


def test_a_mac_server_comes_back_through_its_supervisor_not_a_task_or_a_command(monkeypatch):
    monkeypatch.setattr(admin_api, 'server_module', lambda: 'app.service.macos')
    monkeypatch.setattr(admin_api, 'scheduled_task', lambda: pytest.fail('That is for Windows'))
    assert admin_api.restart_plan() == {'macos': True}


def test_pg_dump_is_found_where_the_mac_installer_recorded_it(world, monkeypatch, tmp_path):
    tools = tmp_path / 'brew/bin'
    tools.mkdir(parents=True)
    pg_dump = 'pg_dump.exe' if os.name == 'nt' else 'pg_dump'
    (tools / pg_dump).write_bytes(b'fixture')
    receipt = tmp_path / 'root/runtime'
    receipt.mkdir(parents=True)
    (receipt / 'macos-preparation.json').write_text(json.dumps({'tools': {'postgres': str(tools / 'postgres')}}))
    monkeypatch.setattr(admin_runs, 'ROOT', tmp_path / 'root')
    monkeypatch.setattr(admin_runs, 'DATA', tmp_path / 'elsewhere/data')
    monkeypatch.setattr(admin_runs.shutil, 'which', lambda name: None)
    assert admin_runs.find_pg_dump({}) == tools / pg_dump
    (receipt / 'macos-preparation.json').write_text('{}')
    assert admin_runs.find_pg_dump({}) is None


@pytest.fixture
def mac_runner(world, monkeypatch):
    marker = world.tmp / 'runtime/macos-web-pause'
    marker.parent.mkdir(exist_ok=True)
    monkeypatch.setattr(admin_runner, 'PAUSE_WEB', marker)
    monkeypatch.setattr(admin_runner.time, 'sleep', lambda seconds: None)
    return marker


def test_a_mac_backup_asks_the_supervisor_to_stop_only_the_web_server(world, mac_runner, monkeypatch):
    run = admin_runner.Run(record(world, server_pid=4242, restart={'macos': True})['id'])
    alive = iter([True, True, False])
    monkeypatch.setattr(admin_runner, 'process_alive', lambda pid: pid == 4242 and next(alive))
    admin_runner.stop_application(run)
    request = json.loads(mac_runner.read_text())
    assert request == {'run': run.value['id'], 'pid': os.getpid()}
    assert not (world.data / 'DESKTOP-SERVER-STOP').exists(), 'that file is the Windows signal'


def test_a_mac_server_that_does_not_stop_is_released_and_the_backup_does_not_start(world, mac_runner, monkeypatch):
    run = admin_runner.Run(record(world, server_pid=4242, restart={'macos': True})['id'])
    monkeypatch.setattr(admin_runner, 'process_alive', lambda pid: True)
    ticks = iter(range(0, 10 ** 6, 40))
    monkeypatch.setattr(admin_runner.time, 'monotonic', lambda: next(ticks))
    with pytest.raises(RuntimeError, match='did not stop in time'):
        admin_runner.stop_application(run)
    assert not mac_runner.exists(), 'the web server must come back'


def test_a_mac_restart_removes_the_pause_and_starts_nothing_itself(world, mac_runner, monkeypatch):
    mac_runner.write_text('{}')
    monkeypatch.setattr(admin_runner.subprocess, 'Popen', lambda *a, **k: pytest.fail('The supervisor starts it'))
    run = admin_runner.Run(record(world, restart={'macos': True})['id'])
    admin_runner.restart_application(run)
    assert not mac_runner.exists()


def test_a_crashed_mac_backup_brings_the_application_back_unless_it_holds_the_gate(world, mac_runner):
    run_id = record(world, restart={'macos': True})['id']
    mac_runner.write_text('{}')
    gate = world.data / '.recovery-admission/maintenance.json'
    gate.parent.mkdir(parents=True)
    gate.write_text('{}')
    admin_runner.recover_from_crash(run_id, 'boom')
    assert mac_runner.exists() and admin_runs.read_run(run_id)['state'] == 'failed'
    gate.unlink()
    admin_runner.recover_from_crash(run_id, 'boom')
    assert not mac_runner.exists() and admin_runs.read_run(run_id)['state'] == 'finished'


# One pass, the database rules, and the time estimate.

def test_a_run_from_the_page_captures_in_one_pass(api, world):
    """The run verifies every copy afterwards, so the capture doesn't read them back too."""
    assert api.client.post('/api/backup/start', json=start_body(world)).status_code == 200
    assert admin_runs.read_run(api.launched[0])['single_pass'] is True


@pytest.mark.parametrize('recorded, expected', [(True, True), (None, False)])
def test_the_runner_captures_in_the_mode_its_run_recorded(world, monkeypatch, recorded, expected):
    """A run recorded before one pass existed is resumed the way it started."""
    seen = {}

    def capture(*args, **kwargs):
        seen.update(kwargs)
        return {'id': 'c', 'media_files': 1, 'media_bytes': 1}
    monkeypatch.setattr(admin_runner, 'capture_library', capture)
    monkeypatch.setattr(admin_runner, 'verify_library', lambda directory, key, progress: {'id': 'c', 'media_files': 1, 'media_bytes': 1})
    monkeypatch.setattr(admin_runner, 'verify_library_thumbnails', lambda *args: None)
    value = {'directory': str(world.backup / 'new'), 'pg_dump': str(world.pg_dump),
             'database_budget': 1024, 'allow_unprotected': False}
    if recorded is not None:
        value['single_pass'] = recorded
    run = SimpleNamespace(value=value, progress=None, update=lambda **changes: None, note=lambda message: None)
    admin_runner.capture(run, world.config, backup_key())
    assert seen['single_pass'] is expected


def test_database_state_without_a_rule_stops_the_plan_before_anything_stops(api, world, monkeypatch):
    monkeypatch.setattr(admin_api, 'unclassified_database', lambda db: ['The database schema stray has no backup rule.'])
    plan = api.client.post('/api/backup/plan', json=start_body(world)).json()
    assert 'The database schema stray has no backup rule.' in plan['problems']
    assert api.client.post('/api/backup/start', json=start_body(world)).status_code == 409 and not api.launched


class Catalog:
    """The two inventory queries, answered from fixed rows."""
    def __init__(self, schemas, relations):
        self.rows = {admin_runs.SCHEMAS: [{'name': name} for name in schemas],
                     admin_runs.RELATIONS: [{'schema': s, 'name': n, 'kind': k} for s, n, k in relations]}

    def execute(self, query):
        return SimpleNamespace(fetchall=lambda: self.rows[query])

    def commit(self):
        pass


def test_every_schema_and_table_without_a_rule_is_found_at_once():
    found = admin_runs.unclassified_database(Catalog(
        ['people', 'public', 'fftest_' + 'a' * 32],
        [('public', 'media', 'r'), ('public', 'stray_table', 'r'), ('public', 'some_view', 'v'),
         ('people', 'face_detections', 'r'), ('fftest_' + 'a' * 32, 'media', 'r')]))
    assert len(found) == 3
    assert 'fftest_' + 'a' * 32 in found[0]
    assert any('public.stray_table' in text for text in found) and any('public.some_view' in text for text in found)


def test_a_library_whose_database_has_rules_for_everything_has_no_problem():
    assert admin_runs.unclassified_database(Catalog(['public', 'people'], [('public', 'media', 'r')])) == []


def test_the_estimate_comes_from_the_latest_verified_backup_scaled_by_size():
    runs = [{'state': 'finished', 'started_at': '2026-11-10T00:00:00+00:00'},
            {'state': 'finished', 'started_at': '2026-11-09T00:00:00+00:00',
             'verified_at': '2026-11-09T01:00:00+00:00', 'media_bytes': 1000},
            {'state': 'finished', 'started_at': '2026-11-01T00:00:00+00:00',
             'verified_at': '2026-11-01T09:00:00+00:00', 'media_bytes': 1000}]
    estimate = admin_runs.estimate_seconds(runs, 2000)
    assert estimate['last_seconds'] == 3600 and estimate['seconds'] == 7200
    assert estimate['from_media_bytes'] == 1000
    assert admin_runs.estimate_seconds(runs[:1], 2000) is None


def test_the_estimate_prefers_a_one_pass_run_over_a_newer_three_pass_one():
    three_pass = {'started_at': '2026-11-10T00:00:00+00:00', 'verified_at': '2026-11-10T03:00:00+00:00',
                  'media_bytes': 1000}
    one_pass = {'started_at': '2026-11-09T00:00:00+00:00', 'verified_at': '2026-11-09T02:00:00+00:00',
                'media_bytes': 1000, 'single_pass': True}
    assert admin_runs.estimate_seconds([three_pass, one_pass], 1000)['last_seconds'] == 7200
    # With no one-pass run yet, the newest verified run is used.
    assert admin_runs.estimate_seconds([three_pass], 1000)['last_seconds'] == 10800


def test_the_overview_and_the_plan_carry_the_estimate(api, world):
    record(world, state='finished', started_at='2026-11-09T00:00:00+00:00',
           verified_at='2026-11-09T02:00:00+00:00', media_bytes=50)
    assert api.client.get('/api/backup').json()['estimate']['seconds'] == 14400
    assert api.client.post('/api/backup/plan', json=start_body(world, copy_strategy='standard')).json()['estimate']['seconds'] == 14400
    assert api.client.post('/api/backup/plan', json=start_body(world)).json()['estimate'] is None


def test_a_mac_backup_keeps_the_mac_awake_for_the_runners_life(monkeypatch):
    started = []
    monkeypatch.setattr(admin_runner.sys, 'platform', 'darwin')
    monkeypatch.setattr(admin_runner.subprocess, 'Popen', lambda command, **options: started.append(command) or 'process')
    assert admin_runner.keep_awake() == 'process'
    command = started[0]
    assert command[0] == '/usr/bin/caffeinate' and command[-2:] == ['-w', str(os.getpid())]
    assert {'-i', '-s', '-m'} <= set(command) and '-d' not in command  # The display may sleep.


def test_keeping_awake_never_stops_a_backup(monkeypatch, capsys):
    monkeypatch.setattr(admin_runner.subprocess, 'Popen', lambda *a, **k: pytest.fail('started'))
    monkeypatch.setattr(admin_runner.sys, 'platform', 'linux')
    assert admin_runner.keep_awake() is None

    def missing(*a, **k):
        raise FileNotFoundError('caffeinate')
    monkeypatch.setattr(admin_runner.sys, 'platform', 'darwin')
    monkeypatch.setattr(admin_runner.subprocess, 'Popen', missing)
    assert admin_runner.keep_awake() is None
    assert 'may sleep' in capsys.readouterr().err


class Kernel32:
    """A stand-in for Windows' kernel32, recording each request."""

    def __init__(self, result=0x80000000):
        self.calls, self.result = [], result

    def SetThreadExecutionState(self, flags):
        self.calls.append(flags)
        return self.result


def test_a_windows_backup_asks_windows_to_stay_awake_and_lets_it_go():
    kernel32 = Kernel32()
    awake = admin_runner.WindowsAwake(kernel32)
    assert awake.hold() and awake.poll() is None
    awake.terminate()
    # ES_CONTINUOUS | ES_SYSTEM_REQUIRED, then ES_CONTINUOUS alone: the system may sleep again.
    # Never ES_DISPLAY_REQUIRED (2): the display may still turn off.
    assert kernel32.calls == [0x80000001, 0x80000000] and awake.poll() == 0
    awake.terminate()
    assert len(kernel32.calls) == 2  # Letting go twice asks nothing more.


def test_a_refused_request_is_reported_not_fatal(monkeypatch, capsys):
    monkeypatch.setattr(admin_runner.sys, 'platform', 'win32')
    refused = admin_runner.WindowsAwake(Kernel32(result=0))
    assert refused.hold() is False and refused.poll() == 0
    import ctypes
    monkeypatch.setattr(ctypes, 'windll', SimpleNamespace(kernel32=Kernel32(result=0)), raising=False)
    assert admin_runner.keep_awake() is None
    assert 'may sleep' in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows power request')
def test_windows_really_holds_the_request_until_it_is_let_go():
    import ctypes
    kernel32 = ctypes.windll.kernel32
    awake = admin_runner.keep_awake()
    try:
        assert isinstance(awake, admin_runner.WindowsAwake)
        # The call returns the thread's previous state, so asking again shows what is held.
        assert kernel32.SetThreadExecutionState(0x80000001) & 0x80000001 == 0x80000001
    finally:
        awake.terminate()
    assert kernel32.SetThreadExecutionState(0x80000000) & 0x00000001 == 0


def test_the_runner_says_it_keeps_windows_awake(monkeypatch):
    notes = []
    monkeypatch.setattr(admin_runner, 'keep_awake', lambda: admin_runner.WindowsAwake(Kernel32()))
    monkeypatch.setattr(admin_runner, 'run_backup', lambda run_id, key=None: None)
    monkeypatch.setattr(admin_runner, 'note_activity_safely', lambda run_id, message: notes.append(message))
    monkeypatch.setattr(admin_runner, 'ActivityHandler', lambda run_id: logging.NullHandler())
    monkeypatch.setattr(admin_runner.sys, 'platform', 'win32')
    assert admin_runner.main(['--run-id', '20261112T000000Z-0123abcd']) == 0
    assert any('Keeping this computer awake' in note for note in notes)


def test_the_runner_lets_the_mac_sleep_again_when_it_ends(monkeypatch):
    class Awake:
        stopped = False

        def poll(self):
            return None

        def terminate(self):
            Awake.stopped = True
    notes = []
    monkeypatch.setattr(admin_runner, 'keep_awake', Awake)
    monkeypatch.setattr(admin_runner, 'run_backup', lambda run_id, key=None: None)
    monkeypatch.setattr(admin_runner, 'note_activity_safely', lambda run_id, message: notes.append(message))
    monkeypatch.setattr(admin_runner, 'ActivityHandler', lambda run_id: logging.NullHandler())
    monkeypatch.setattr(admin_runner.sys, 'platform', 'darwin')
    assert admin_runner.main(['--run-id', '20261112T000000Z-0123abcd']) == 0 and Awake.stopped
    assert any('Keeping the Mac awake' in note for note in notes)  # The run's log says so.


def test_a_runner_started_again_unlocks_the_existing_capture_context(tmp_path, monkeypatch):
    """A relaunched runner for an interrupted capture must derive the context's own key, not a new one."""
    from app.backup import passwords
    from tests.backup_keys import ADMIN_PASSWORD
    folder = tmp_path / 'capture'
    folder.mkdir()
    password = passwords.BackupPassword(ADMIN_PASSWORD)
    original = passwords.create_context(folder, password)
    monkeypatch.setattr(admin_runner, 'read_run', lambda run_id: {'directory': str(folder)})
    again = admin_runner.key_for_run('20261112T000000Z-0123abcd', password)
    assert bytes(again) == bytes(original) and again.binding() == original.binding()
    with pytest.raises(passwords.WrongPassword):
        admin_runner.key_for_run('20261112T000000Z-0123abcd', passwords.BackupPassword('another password entirely'))
    # A genuinely new capture (no context in its folder yet) gets a new key.
    (tmp_path / 'fresh').mkdir()
    monkeypatch.setattr(admin_runner, 'read_run', lambda run_id: {'directory': str(tmp_path / 'fresh')})
    assert admin_runner.key_for_run('20261112T000000Z-0123abcd', password).binding() != original.binding()
