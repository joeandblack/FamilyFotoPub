"""Testing a restore from the admin page: never near the library, and always cleaned up after."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import psycopg
from fastapi.testclient import TestClient
import pytest

from app.backup import admin_api, admin_restore, admin_runs
from tests.test_backup_admin import api, record, world  # noqa: F401
from tests.backup_keys import backup_key


PG_BIN = os.environ.get('FAMILYFOTO_BACKUP_PG_BIN')


@pytest.fixture
def restores(world, api, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setattr(admin_restore, 'RESTORES', tmp_path / 'logs/backup-restores')
    launched = []
    monkeypatch.setattr(admin_restore, 'launch', lambda run_id, root, password: launched.append(run_id))
    tools = tmp_path / 'tools'
    tools.mkdir()
    for name in ('pg_dump', 'initdb', 'pg_ctl', 'pg_restore'):
        (tools / (name + admin_restore.executable_suffix())).write_bytes(b'fixture')
    world.config['backup']['pg_dump'] = str(tools / ('pg_dump' + admin_restore.executable_suffix()))
    backup = world.backup / 'familyfoto-backup-fixture'
    (backup / 'library').mkdir(parents=True)
    (backup / 'library/complete.fernet').write_bytes(b'sealed')
    parent = tmp_path / 'restores'
    parent.mkdir()
    return SimpleNamespace(launched=launched, backup=backup, parent=parent, tools=tools,
                           body={'backup_directory': str(backup), 'password': world.password, 'parent': str(parent)})


def start(api, body):  # noqa: F811
    return api.client.post('/api/backup/restore', json=body)


@pytest.mark.parametrize('method, path', [('get', '/api/backup/restores'), ('post', '/api/backup/restore'),
                                          ('post', '/api/backup/restore/20261104T000000Z-00000000/retry')])
@pytest.mark.parametrize('role', ['regular', 'guest'])
def test_only_an_administrator_can_test_a_restore(api, restores, method, path, role):  # noqa: F811
    api.role.value = role
    response = getattr(api.client, method)(path, **({'json': {}} if method == 'post' else {}))
    assert response.status_code == 403
    assert not restores.launched


def test_a_restore_starts_in_a_new_private_folder(api, restores):  # noqa: F811
    response = start(api, restores.body)
    assert response.status_code == 200
    run = admin_restore.read_run(restores.launched[0])
    folder = Path(run['directory'])
    assert folder.is_dir() and folder.parent == restores.parent and not list(folder.iterdir())
    assert run['state'] == 'starting' and run['backup_directory'] == str(restores.backup)
    assert run['tools'] == str(restores.tools)


def test_the_application_is_not_stopped_by_a_restore(api, restores, world):  # noqa: F811
    start(api, restores.body)
    assert not (world.data / 'DESKTOP-SERVER-STOP').exists()
    assert not admin_runs.all_runs()


@pytest.mark.parametrize('place', ['media', 'data', 'config'])
def test_a_restore_is_refused_inside_the_captured_folders(api, restores, world, place):  # noqa: F811
    response = start(api, {**restores.body, 'parent': str(world.tmp / place)})
    assert response.status_code == 409 and 'outside' in response.json()['detail'] and not restores.launched


def test_a_folder_holding_the_library_is_refused(api, restores, world, tmp_path_factory):  # noqa: F811
    """With the backup elsewhere, so only the library's own check can refuse this."""
    backup = tmp_path_factory.mktemp('elsewhere') / 'familyfoto-backup-fixture'
    (backup / 'library').mkdir(parents=True)
    (backup / 'library/complete.fernet').write_bytes(b'sealed')
    response = start(api, {**restores.body, 'backup_directory': str(backup), 'parent': str(world.tmp)})
    assert response.status_code == 409 and 'library' in response.json()['detail']


def test_a_restore_is_refused_inside_the_backup(api, restores):  # noqa: F811
    response = start(api, {**restores.body, 'parent': str(restores.backup)})
    assert response.status_code == 409 and 'backup folder' in response.json()['detail']


def test_a_folder_that_is_not_a_finished_backup_is_refused(api, restores, world):  # noqa: F811
    response = start(api, {**restores.body, 'backup_directory': str(world.tmp)})
    assert response.status_code == 409 and 'not a finished backup' in response.json()['detail']


def test_a_missing_folder_is_refused(api, restores):  # noqa: F811
    assert start(api, {**restores.body, 'parent': str(restores.parent / 'missing')}).status_code == 409


def test_a_missing_password_is_refused(api, restores, world):  # noqa: F811
    response = start(api, {**restores.body, 'password': ''})
    assert response.status_code == 409 and 'administrator password' in response.json()['detail'] and not restores.launched


def test_a_wrong_password_ends_the_run_before_anything_is_written(api, restores, monkeypatch):  # noqa: F811
    from app.backup.password_handoff import HandoffFailed

    def refuse(run_id, root, password):
        raise HandoffFailed("The password is incorrect, or the backup's password information is damaged.")
    monkeypatch.setattr(admin_restore, 'launch', refuse)
    response = start(api, {**restores.body, 'password': 'not the right password'})
    assert response.status_code == 409 and 'incorrect' in response.json()['detail']
    assert 'not the right password' not in response.text
    run = admin_restore.all_runs()[0]
    assert run['state'] == 'failed' and 'not the right password' not in json.dumps(run)


def test_an_unprotected_disk_is_refused_with_no_override(api, restores, monkeypatch):  # noqa: F811
    def refuse(path):
        raise admin_api.UnprotectedDestination('No permissions')
    monkeypatch.setattr(admin_api, 'check_destination_privacy', refuse)
    response = start(api, {**restores.body, 'allow_unprotected': True})
    assert response.status_code == 409 and 'private' in response.json()['detail'] and not restores.launched


def test_too_little_space_is_refused(api, restores, monkeypatch):  # noqa: F811
    monkeypatch.setattr(admin_api.shutil, 'disk_usage', lambda path: SimpleNamespace(free=1))
    assert start(api, restores.body).status_code == 409


@pytest.mark.parametrize('missing', ['initdb', 'pg_ctl', 'pg_restore'])
def test_missing_postgresql_tools_are_named(api, restores, missing):  # noqa: F811
    (restores.tools / (missing + admin_restore.executable_suffix())).unlink()
    response = start(api, restores.body)
    assert response.status_code == 409 and missing in response.json()['detail']


def live_run(monkeypatch, directory='x', **changes):
    monkeypatch.setattr(admin_restore, 'process_alive', lambda pid: True)
    return admin_restore.write_run({'id': '20261104T000000Z-00000000', 'state': 'restoring', 'runner_pid': 4242,
                                    'started_at': admin_runs.now(), 'directory': directory, 
                                    'tools': 'tools', **changes})


def test_only_one_restore_runs_at_once(api, restores, monkeypatch):  # noqa: F811
    live_run(monkeypatch)
    assert start(api, restores.body).status_code == 409 and not restores.launched


def test_a_restore_is_refused_while_a_backup_runs(api, restores, world, monkeypatch):  # noqa: F811
    monkeypatch.setattr(admin_runs, 'process_alive', lambda pid: True)
    record(world, state='capturing', runner_pid=4242)
    response = start(api, restores.body)
    assert response.status_code == 409 and 'backup' in response.json()['detail'].lower()


def test_a_backup_is_refused_while_a_restore_runs(api, restores, world, monkeypatch):  # noqa: F811
    live_run(monkeypatch)
    response = api.client.post('/api/backup/plan', json={'destination': str(world.backup), 'password': world.password})
    assert any('restore test' in problem for problem in response.json()['problems'])


def test_two_restores_at_once_launch_one(api, restores, monkeypatch):  # noqa: F811
    real = admin_api.restore_plan
    both_inside, results = threading.Barrier(2, timeout=5), []

    def slow(*args):
        try:
            both_inside.wait()
        except threading.BrokenBarrierError:
            pass
        return real(*args)
    monkeypatch.setattr(admin_api, 'restore_plan', slow)

    def post():
        results.append(TestClient(api.client.app, base_url='https://testserver').post(
            '/api/backup/restore', json=restores.body).status_code)
    threads = [threading.Thread(target=post) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(20)
    assert sorted(results) == [200, 409] and len(restores.launched) == 1


def test_only_verified_backups_are_offered(api, restores, world):  # noqa: F811
    record(world, id='20261104T000000Z-00000001', state='finished', verified_at=admin_runs.now(), media_files=3,
           directory=str(restores.backup))
    record(world, id='20261104T000000Z-00000002', state='finished', gave_up=True, directory=str(restores.backup))
    backups = api.client.get('/api/backup/restores').json()['backups']
    assert len(backups) == 1 and backups[0]['media_files'] == 3


def test_a_backup_whose_folder_is_gone_is_not_offered(api, restores, world, tmp_path):  # noqa: F811
    record(world, id='20261104T000000Z-00000001', state='finished', verified_at=admin_runs.now(),
           directory=str(tmp_path / 'deleted-backup'))
    assert api.client.get('/api/backup/restores').json()['backups'] == []


def test_a_listing_never_shows_the_key_path(api, restores, monkeypatch):  # noqa: F811
    live_run(monkeypatch)
    shown = api.client.get('/api/backup/restores').json()['runs'][0]
    assert not {'tools', 'runner_pid'} & set(shown)


def test_a_restore_whose_process_died_is_reported_as_failed(api, restores, monkeypatch):  # noqa: F811
    live_run(monkeypatch)
    monkeypatch.setattr(admin_restore, 'process_alive', lambda pid: False)
    shown = api.client.get('/api/backup/restores').json()['runs'][0]
    assert shown['state'] == 'failed' and 'stopped before' in shown['error']
    assert start(api, restores.body).status_code == 200


def test_a_failed_restore_can_be_retried(api, restores, monkeypatch):  # noqa: F811
    run = live_run(monkeypatch, state='failed', error='Disk full')
    assert api.client.post(f"/api/backup/restore/{run['id']}/retry", json={}).status_code == 409  # Asked again.
    response = api.client.post(f"/api/backup/restore/{run['id']}/retry", json={'password': 'the password'})
    assert response.status_code == 200 and restores.launched == [run['id']]
    saved = admin_restore.read_run(run['id'])
    assert saved['state'] == 'starting' and saved['error'] is None and 'runner_pid' not in saved


@pytest.mark.parametrize('state', ['finished', 'restoring'])
def test_only_a_failed_restore_can_be_retried(api, restores, monkeypatch, state):  # noqa: F811
    run = live_run(monkeypatch, state=state)
    assert api.client.post(f"/api/backup/restore/{run['id']}/retry", json={'password': 'x'}).status_code == 409
    assert not restores.launched


@pytest.mark.parametrize('bad', ['..%2f..%2fx', 'nope'])
def test_a_retry_of_an_unknown_run_is_refused(api, restores, bad):  # noqa: F811
    assert api.client.post(f'/api/backup/restore/{bad}/retry', json={'password': 'x'}).status_code in {404, 405}


class FakeCluster:
    instances = []

    def __init__(self, directory, tools, environment):
        self.data, self.events = directory / 'database', []
        self.environment = environment
        FakeCluster.instances.append(self)

    def start(self):
        self.events.append('start')
        if self.fail_start:
            raise RuntimeError('Could not start')

    def stop(self):
        self.events.append('stop')

    def endpoint(self):
        self.events.append('endpoint')
        return object()

    fail_start = False


@pytest.fixture
def flow(world, restores, monkeypatch, tmp_path):  # noqa: F811
    FakeCluster.instances, FakeCluster.fail_start = [], False
    folder = tmp_path / 'restores/familyfoto-restore-fixture'
    folder.mkdir()
    run_value = admin_restore.write_run({
        'id': '20261104T000000Z-00000000', 'state': 'starting', 'started_at': admin_runs.now(), 'directory': str(folder),
        'backup_directory': str(restores.backup), 'tools': str(restores.tools),
        'database_budget': 1024})
    import app.migration.credentials as credentials
    import app.migration.tls as tls
    import app.migration.validation as validation
    calls, options = [], {}
    monkeypatch.setattr(admin_restore, 'RecoveryCluster', FakeCluster)
    monkeypatch.setattr(admin_restore, 'verify_library_preflight', lambda *a: calls.append('verify') or {'id': 'c' * 32})
    monkeypatch.setattr(admin_restore, 'verify_artifact', lambda *a: {'metadata': {'compatibility': {'environment': {
        'encoding': 6, 'datcollate': 'C', 'datctype': 'C'}}}})

    def restored(backup, key, target, admin, pg_restore, **kwargs):
        calls.append(('restore', kwargs['resume']))
        options['validation_follows'] = kwargs['validation_follows']
        (target / 'catalog').mkdir(parents=True)
        (target / 'catalog/database-target.json').write_text('{}')
        return {'media_files': 2, 'media_bytes': 9}
    monkeypatch.setattr(admin_restore, 'restore_library', restored)
    monkeypatch.setattr(credentials, 'prepare_recovery_role', lambda *a, **k: calls.append('role'))
    monkeypatch.setattr(tls, 'prepare_recovery_tls', lambda *a, **k: calls.append('tls'))
    result = {'activation_allowed': False, 'services_started': False}

    def validated(*args, **kwargs):
        options['fresh_restore'] = kwargs['fresh_restore']
        calls.append('validate')
        return result
    monkeypatch.setattr(validation, 'validate_plain_candidate', validated)
    return SimpleNamespace(run=admin_restore.Run(run_value['id']), calls=calls, options=options, result=result, folder=folder)


def test_a_restore_validates_then_stops_its_database(flow):
    admin_restore.restore(flow.run, backup_key())
    assert flow.calls == ['verify', ('restore', False), 'role', 'tls', 'validate']
    assert flow.options == {'validation_follows': True, 'fresh_restore': True}
    saved = admin_restore.read_run(flow.run.value['id'])
    assert saved['state'] == 'finished' and saved['validated_at'] and saved['media_files'] == 2
    assert FakeCluster.instances[0].events[-1] == 'stop'


def test_the_temporary_database_matches_the_sources_locale(flow):
    admin_restore.restore(flow.run, backup_key())
    assert FakeCluster.instances[0].environment == {'encoding': 'UTF8', 'collate': 'C', 'ctype': 'C'}


def test_the_database_stops_even_when_starting_it_fails(flow):
    FakeCluster.fail_start = True
    with pytest.raises(RuntimeError, match='Could not start'):
        admin_restore.restore(flow.run, backup_key())
    assert FakeCluster.instances[0].events == ['start', 'stop']


def test_the_database_stops_when_validation_fails(flow, monkeypatch):
    import app.migration.validation as validation

    def refuse(*args, **kwargs):
        raise ValueError('Recovery candidate identity differs')
    monkeypatch.setattr(validation, 'validate_plain_candidate', refuse)
    with pytest.raises(ValueError):
        admin_restore.restore(flow.run, backup_key())
    assert FakeCluster.instances[0].events[-1] == 'stop'
    assert 'validated_at' not in admin_restore.read_run(flow.run.value['id'])


@pytest.mark.parametrize('flag', ['activation_allowed', 'services_started'])
def test_a_copy_that_is_not_isolated_is_not_reported_as_good(flow, flag):
    flow.result[flag] = True
    with pytest.raises(RuntimeError, match='not isolated'):
        admin_restore.restore(flow.run, backup_key())
    assert 'validated_at' not in admin_restore.read_run(flow.run.value['id'])


def test_a_retry_continues_what_is_there(flow):
    (flow.folder / 'library').mkdir()
    (flow.folder / 'library/restore-job.json').write_text('{}')
    (flow.folder / 'role').mkdir()
    (flow.folder / 'role/credentials.json').write_text('{}')
    (flow.folder / 'tls').mkdir()
    (flow.folder / 'tls/TLS-REPORT.json').write_text('{}')
    admin_restore.restore(flow.run, backup_key())
    assert ('restore', True) in flow.calls and 'tls' not in flow.calls


def test_a_failure_is_recorded_for_the_page(flow, monkeypatch):
    def refuse(run, key):
        raise RuntimeError('Disk full')
    monkeypatch.setattr(admin_restore, 'restore', refuse)
    monkeypatch.setattr(admin_restore.password_handoff, 'receive', lambda: 'the password')
    monkeypatch.setattr(admin_restore.password_handoff, 'ready', lambda: None)
    monkeypatch.setattr(admin_restore.passwords, 'unlock_context', lambda folder, password: backup_key())
    assert admin_restore.main(['--run-id', flow.run.value['id'], '--backup-password-frame-stdin']) == 1
    saved = admin_restore.read_run(flow.run.value['id'])
    assert saved['state'] == 'failed' and saved['error'] == 'Disk full' and saved['runner_pid'] == os.getpid()


def test_the_runner_takes_no_application_lease():
    code = Path(admin_restore.__file__).read_text(encoding='utf-8')
    assert 'load_config' not in code.replace("never loads the application's settings", '')
    assert 'hold_process_lease' not in code and 'initialize_runtime' not in code


def test_no_route_can_activate_a_restored_copy():
    source = Path(admin_api.__file__).read_text(encoding='utf-8')
    assert 'commit_activation' not in source and 'prepare_activation' not in source


@pytest.mark.skipif(not PG_BIN, reason='Set FAMILYFOTO_BACKUP_PG_BIN to real PostgreSQL tools')
def test_a_real_temporary_database_starts_needs_its_password_and_stops(tmp_path):
    """Real tools, because on Windows the server inherits pg_ctl's output and a pipe never closes."""
    tools = Path(PG_BIN)
    cluster = admin_restore.RecoveryCluster(tmp_path, tools, {'encoding': 'UTF8', 'collate': 'C', 'ctype': 'C'})
    cluster.create()
    assert 'scram-sha-256' in (tmp_path / 'database/pg_hba.conf').read_text()
    cluster.start()
    try:
        endpoint = cluster.endpoint()
        with psycopg.connect(**endpoint.connection()) as db:
            listening = db.execute('SHOW listen_addresses').fetchone()[0]
            assert listening == '127.0.0.1'
        with pytest.raises(psycopg.OperationalError):
            psycopg.connect(host='127.0.0.1', port=cluster.port, dbname='postgres', user=admin_restore.ADMIN,
                            password='wrong', connect_timeout=5)
    finally:
        cluster.stop()
    assert not (tmp_path / 'database/postmaster.pid').exists()
    probe = subprocess.run([str(tools / ('pg_isready' + admin_restore.executable_suffix())), '-h', '127.0.0.1',
                            '-p', str(cluster.port), '-q'], timeout=30)
    assert probe.returncode != 0


@pytest.mark.skipif(not PG_BIN, reason='Set FAMILYFOTO_BACKUP_PG_BIN to real PostgreSQL tools')
def test_a_real_temporary_database_can_be_started_again(tmp_path):
    cluster = admin_restore.RecoveryCluster(tmp_path, Path(PG_BIN), {'encoding': 'UTF8', 'collate': 'C', 'ctype': 'C'})
    for _ in range(2):
        cluster.start()
        try:
            with psycopg.connect(**cluster.endpoint().connection()) as db:
                assert db.execute('SELECT 1').fetchone()[0] == 1
        finally:
            cluster.stop()


KILLED_RUNNER = '''
import sys, time
from pathlib import Path
from app.backup.admin_restore import RecoveryCluster
cluster = RecoveryCluster(Path(sys.argv[1]), sys.argv[2], {'encoding': 'UTF8', 'collate': 'C', 'ctype': 'C'})
cluster.start()
(Path(sys.argv[1]) / 'started').write_text('yes')
time.sleep(600)
'''


@pytest.fixture
def killed_runner(tmp_path):
    """A real runner, killed alone. Its temporary database carries on with nobody to stop it."""
    folder = tmp_path / 'restore'
    folder.mkdir()
    cluster = admin_restore.RecoveryCluster(folder, Path(PG_BIN), {'encoding': 'UTF8', 'collate': 'C', 'ctype': 'C'})
    environment = {**os.environ, **{f'FAMILYFOTO_{name}_DIR': str(tmp_path / 'runtime' / name)
                                    for name in ('DATA', 'LOG', 'TEMP', 'CONFIG')}}
    runner = subprocess.Popen([sys.executable, '-c', KILLED_RUNNER, str(folder), PG_BIN], env=environment,
                              cwd=Path(admin_restore.__file__).parents[2], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **admin_runs.detached_options())
    try:
        deadline = time.monotonic() + 120
        while not (folder / 'started').exists():
            assert runner.poll() is None and time.monotonic() < deadline, 'The runner did not start its database'
            time.sleep(0.2)
        runner.kill()
        runner.wait(timeout=30)
        assert cluster.running()
        yield SimpleNamespace(cluster=cluster, folder=folder, pid=runner.pid)
    finally:
        if runner.poll() is None:
            runner.kill()
            runner.wait(timeout=30)
        cluster.stop()


@pytest.mark.skipif(not PG_BIN, reason='Set FAMILYFOTO_BACKUP_PG_BIN to real PostgreSQL tools')
def test_a_retry_replaces_the_database_a_killed_runner_left(killed_runner):
    retry = admin_restore.RecoveryCluster(killed_runner.folder, Path(PG_BIN), {'encoding': 'UTF8', 'collate': 'C', 'ctype': 'C'})
    retry.start()
    try:
        with psycopg.connect(**retry.endpoint().connection()) as db:
            assert db.execute('SELECT 1').fetchone()[0] == 1
    finally:
        retry.stop()
    assert not retry.running()


@pytest.mark.skipif(not PG_BIN, reason='Set FAMILYFOTO_BACKUP_PG_BIN to real PostgreSQL tools')
def test_finding_a_killed_runner_stops_its_database(killed_runner, monkeypatch, tmp_path):
    monkeypatch.setattr(admin_restore, 'RESTORES', tmp_path / 'runs')
    admin_restore.write_run({'id': admin_restore.new_run_id(), 'state': 'restoring', 'started_at': admin_runs.now(),
                             'directory': str(killed_runner.folder), 'tools': PG_BIN, 'runner_pid': killed_runner.pid})
    assert admin_restore.all_runs()[0]['state'] == 'failed'
    assert not killed_runner.cluster.running()


def test_a_killed_runner_is_still_reported_if_its_database_cant_be_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(admin_restore, 'RESTORES', tmp_path / 'runs')
    monkeypatch.setattr(admin_restore, 'runner_gone', lambda run: True)

    def refuse(self):
        raise subprocess.TimeoutExpired('pg_ctl', 1)
    monkeypatch.setattr(admin_restore.RecoveryCluster, 'stop', refuse)
    admin_restore.write_run({'id': admin_restore.new_run_id(), 'state': 'restoring', 'started_at': admin_runs.now(),
                             'directory': str(tmp_path), 'tools': str(tmp_path)})
    assert admin_restore.all_runs()[0]['state'] == 'failed'


def test_a_leftover_that_wont_stop_is_named_and_nothing_starts(monkeypatch, tmp_path):
    cluster = admin_restore.RecoveryCluster(tmp_path, tmp_path, {})
    (tmp_path / 'database').mkdir()
    started = []
    monkeypatch.setattr(cluster, 'running', lambda: True)
    monkeypatch.setattr(cluster, 'stop', lambda: None)
    monkeypatch.setattr(cluster, 'run', lambda *a, **k: started.append(a))
    with pytest.raises(RuntimeError, match='still running'):
        cluster.start()
    assert started == []


def test_a_failed_start_reports_the_servers_reason(monkeypatch, tmp_path):
    cluster = admin_restore.RecoveryCluster(tmp_path, tmp_path, {})
    (tmp_path / 'database').mkdir()
    (tmp_path / 'database.log').write_text('FATAL:  an old reason\n')
    monkeypatch.setattr(cluster, 'running', lambda: False)

    def failed(*arguments, **options):
        with (tmp_path / 'database.log').open('a') as log:
            log.write('FATAL:  lock file "postmaster.pid" already exists\n')
        return SimpleNamespace(returncode=1, output='pg_ctl: could not start server\nExamine the log output.\n')
    monkeypatch.setattr(cluster, 'run', failed)
    with pytest.raises(RuntimeError, match='lock file') as caught:
        cluster.start()
    assert 'old reason' not in str(caught.value) and 'Examine' not in str(caught.value)


def test_a_failed_start_ignores_reasons_from_earlier_attempts(monkeypatch, tmp_path):
    cluster = admin_restore.RecoveryCluster(tmp_path, tmp_path, {})
    (tmp_path / 'database').mkdir()
    (tmp_path / 'database.log').write_text('FATAL:  an old reason\n')
    monkeypatch.setattr(cluster, 'running', lambda: False)
    monkeypatch.setattr(cluster, 'run', lambda *a, **k: SimpleNamespace(returncode=1, output='pg_ctl: this reason\n'))
    with pytest.raises(RuntimeError, match='this reason'):
        cluster.start()


@pytest.mark.parametrize('output, reason', [
    ('FATAL:  disk is full\npg_ctl: could not start server\nExamine the log output.\n', 'FATAL:  disk is full'),
    ('FATAL:  disk is full\nLOG:  database system is shut down\n', 'FATAL:  disk is full'),
    ('pg_ctl: could not start server\nExamine the log output.\n', 'exit code 1'),
])
def test_the_line_shown_is_the_one_naming_the_reason(output, reason):
    assert admin_restore.last_line(SimpleNamespace(returncode=1, output=output)) == reason


def test_a_tools_output_is_read_from_this_run_only(tmp_path):
    (tmp_path / 'database-tools.log').write_text('an earlier failure\n')
    script = tmp_path / ('tool.cmd' if os.name == 'nt' else 'tool.sh')
    script.write_text('@echo this run\n' if os.name == 'nt' else '#!/bin/sh\necho this run\n')
    script.chmod(0o700)
    result = admin_restore.RecoveryCluster(tmp_path, tmp_path, {}).run(script.name)
    assert 'this run' in result.output and 'earlier' not in result.output


@pytest.mark.skipif(os.name == 'nt', reason='Windows folders hold the privacy, not the file mode')
def test_the_database_password_is_owner_only(monkeypatch, tmp_path):
    cluster = admin_restore.RecoveryCluster(tmp_path, tmp_path, {'encoding': 'UTF8', 'collate': 'C', 'ctype': 'C'})
    monkeypatch.setattr(cluster, 'run', lambda *a, **k: SimpleNamespace(returncode=0, output=''))
    cluster.create()
    assert cluster.password_file.stat().st_mode & 0o777 == 0o600


@pytest.mark.skipif(os.name == 'nt', reason='Uses the POSIX signal check')
def test_a_process_we_cant_signal_still_counts_as_alive(monkeypatch):
    def refuse(pid, signal):
        raise PermissionError
    monkeypatch.setattr(admin_runs.os, 'kill', refuse)
    assert admin_runs.process_alive(1) is True


def test_a_tool_failure_names_the_reason(tmp_path):
    (tmp_path / 'database-tools.log').write_text('first\ninitdb: could not create\n')
    assert 'could not create' in admin_restore.last_line(SimpleNamespace(
        returncode=1, output=(tmp_path / 'database-tools.log').read_text()))
    assert admin_restore.last_line(SimpleNamespace(returncode=3, output='')) == 'exit code 3'
