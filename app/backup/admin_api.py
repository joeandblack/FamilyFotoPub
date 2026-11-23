"""Administrator routes for backups: check a plan, start a run, follow it, test a restore.

A backup is protected by the administrator password (docs/BACKUP.md, "Backup password"). It
arrives in a request body, is checked here, and goes to the worker on a private pipe; it is
never stored, logged or echoed back."""
import os
import logging
import time
from pathlib import Path
import secrets
import subprocess
import sys
import threading

import shutil

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from ..config import configuration_root, DATA, load_config, LOGS, ROOT
from ..database import connection
from . import admin_restore
from .admin_runs import (all_runs, backup_directory, check_plan, current_run, detached_options, DUMP_ALLOWANCE,
                         estimate_seconds, unclassified_database,
                         find_pg_dump, inside, new_run_id, note_activity_safely, now, public_run, read_activity,
                         read_preferences, save_preferences,
                         MACOS_SERVER, RECOVERED_SERVER, SUPPORTED_SERVERS, token_digest, token_matches, write_run)
from .password_handoff import HandoffFailed, launch as launch_with_password
from .passwords import PROTECTS
from .capture_options import capture_strategy
from .diagnostics import ExplainedFailure
from .private_storage import check_destination_privacy, UnprotectedDestination
from ..service.instance import backup_cookie, DEFAULT as DEFAULT_NAMES, names as instance_names


TASK = DEFAULT_NAMES['server_task']
HISTORY = 20
router = APIRouter()
# One server process serves this page, so this is enough to make checking and starting one step.
starting = threading.Lock()


def require_admin(request):
    if request.state.user['role'] != 'admin':
        raise HTTPException(403, 'Administrator required')


def server_module():
    """The module this server was started as. Only those with a stop signal can hand over."""
    spec = getattr(sys.modules.get('__main__'), '__spec__', None)
    return spec.name if spec else None


def support():
    if sys.platform == 'darwin':
        if server_module() != MACOS_SERVER:
            return 'This server was not started by the FamilyFoto service, so a backup cannot stop and restart it. Use the backup command.'
        return None
    if sys.platform != 'win32':
        return 'Starting a backup from this page is not available on this system yet. Use the backup command.'
    if server_module() not in SUPPORTED_SERVERS:
        return 'This server was not started in a way that lets a backup stop it. Use the backup command.'
    return None


def sizes(db):
    media = db.execute('SELECT coalesce(sum(size),0) AS total FROM media').fetchone()['total']
    database = db.execute('SELECT pg_database_size(current_database()) AS size').fetchone()['size']
    return int(media), int(database)


def media_busy(db):
    from ..jobs import snapshot
    return bool(snapshot(db)['busy'])


def history():
    """What an administrator needs to see of past runs. Never tokens or restart details."""
    return [public_run(run, authorized=True) for run in all_runs()[:HISTORY]]


@router.get('/api/backup')
def overview(request: Request):
    require_admin(request)
    with connection() as db:
        media_bytes, database_bytes = sizes(db)
        busy = media_busy(db)
    return {'unsupported': support(), 'preferences': read_preferences(), 'protects': PROTECTS, 'media_bytes': media_bytes,
            'fastbackup_available': (load_config().get('backup') or {}).get('experimental_fastbackup') is True,
            'database_bytes': database_bytes, 'busy': busy, 'runs': history(),
            'estimate': estimate_seconds(all_runs(), media_bytes)}


def plan_for(config, payload):
    started = time.monotonic()
    logger = logging.getLogger(__name__)
    logger.info('Backup plan check started')
    outcome = 'failed'
    try:
        report = inspect_plan(config, payload)
        report['check_seconds'] = round(time.monotonic() - started, 3)
        outcome = 'blocked' if report['problems'] else 'ready'
        return report
    finally:
        logger.info('Backup plan check %s; total time %.3f seconds', outcome, time.monotonic() - started)


def inspect_plan(config, payload):
    try:
        strategy = capture_strategy(config, payload)
    except ExplainedFailure as error:
        raise HTTPException(422, str(error)) from error
    destination, password = payload.get('destination'), payload.get('password', '')
    if not isinstance(destination, str) or not isinstance(password, str):
        raise HTTPException(422, 'A destination and the administrator password are required')
    with connection() as db:
        media_bytes, database_bytes = sizes(db)
        busy = media_busy(db)
        database_problems = unclassified_database(db)
    report = check_plan(config, destination.strip(), password, media_bytes=media_bytes, database_bytes=database_bytes)
    report['problems'].extend(database_problems)
    report['estimate'] = estimate_seconds(all_runs(), media_bytes)
    report['copy_strategy'] = strategy
    if strategy != 'standard':
        report['estimate'] = None  # Standard-engine timings are not evidence for this engine.
    if busy:
        report['problems'].append('Imports, conversions or scans are running. Pause them or let them finish first.')
    if admin_restore.current_run():
        report['problems'].append('A restore test is running. Wait for it to finish first.')
    return report


@router.post('/api/backup/plan')
def plan(request: Request, payload: dict = Body(...)):
    require_admin(request)
    return plan_for(load_config(), payload)


def restart_plan():
    """How to bring back exactly this server. A test copy must never start the real one."""
    if server_module() == MACOS_SERVER:
        # The service's supervisor brings the web server back when the pause marker goes.
        return {'macos': True}
    if server_module() == RECOVERED_SERVER:
        # DATA is catalog/files/data in the recovery folder. The scheduled task would start the old settings.
        return {'argv': [sys.executable, '-m', RECOVERED_SERVER, '--directory', str(DATA.parents[2])], 'cwd': str(ROOT)}
    task = scheduled_task()
    if task:
        return {'task': task}
    return {'argv': [sys.executable, '-m', server_module()], 'cwd': str(ROOT)}


def server_task():
    """This installation's task: a second installation on the computer has its own (app.service.instance)."""
    return instance_names(load_config())['server_task']


def scheduled_task():
    if sys.platform != 'win32':
        return None
    task = server_task()
    # Matching this copy's own script is what keeps a test copy away from the real task.
    script = str(ROOT / 'scripts/desktop-server.ps1').replace('/', '\\')
    try:
        result = subprocess.run(['powershell', '-NoProfile', '-Command',
                                 f"(Get-ScheduledTask -TaskName '{task}').Actions | ForEach-Object {{ $_.Arguments }}"],
                                capture_output=True, text=True, timeout=30,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.SubprocessError):
        return None
    return task if result.returncode == 0 and script.lower() in result.stdout.lower() else None


def launch_runner(run_id, password):
    """The runner checks the password and makes the backup's key before it stops anything."""
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / 'backup-runner.log').open('ab') as output:
        launch_with_password([sys.executable, '-m', 'app.backup.admin_runner', '--run-id', run_id], password,
                             cwd=ROOT, log=output, **detached_options())


@router.post('/api/backup/start')
def start(request: Request, payload: dict = Body(...)):
    require_admin(request)
    unsupported = support()
    if unsupported:
        raise HTTPException(409, unsupported)
    if payload.get('confirm_stop') is not True:
        raise HTTPException(422, 'Confirm that the application will stop during the backup')
    if not starting.acquire(blocking=False):
        raise HTTPException(409, 'A backup is already starting')
    try:
        return begin_run(request, payload)
    finally:
        starting.release()


def begin_run(request, payload):
    if current_run():
        raise HTTPException(409, 'A backup is already running')
    if admin_restore.current_run():
        raise HTTPException(409, 'A restore operation is already running')
    config = load_config()
    report = plan_for(config, payload)
    if report['problems']:
        return JSONResponse({'detail': report['problems'][0], 'code': 'backup_plan_rejected',
                             'problems': report['problems']}, status_code=409)
    allowed = payload.get('allow_unprotected') is True
    if report['unprotected'] and not allowed:
        message = 'This disk cannot keep files private. Accept that to continue, or choose another disk.'
        return JSONResponse({'detail': message, 'code': 'backup_plan_rejected', 'problems': [message]}, status_code=409)
    destination = payload['destination'].strip()
    token = secrets.token_urlsafe(32)
    run_id = new_run_id()
    write_run({'id': run_id, 'state': 'starting', 'started_at': now(), 'directory': str(backup_directory(destination, run_id)),
               'destination': destination, 'allow_unprotected': allowed,
               # The run verifies every copy afterwards, so the capture needn't read them back too.
               'single_pass': True,
               'copy_strategy': report.get('copy_strategy', 'standard'),
               'verification': payload.get('verification', 'full'),
               'pg_dump': str(find_pg_dump(config)), 'database_budget': report['database_bytes'] * 2 + DUMP_ALLOWANCE,
               'token_sha256': token_digest(token), 'server_pid': os.getpid(), 'restart': restart_plan()})
    save_preferences(destination)
    note_activity_safely(run_id, f'Backup started from Settings, into {backup_directory(destination, run_id)}.')
    note_activity_safely(run_id, f"Backup plan checked: {report.get('check_seconds', 0):.3f} seconds.")
    try:
        launch_runner(run_id, payload['password'])
    except HandoffFailed as error:
        write_run({**read_run_safely(run_id), 'state': 'finished', 'gave_up': True, 'error': str(error),
                   'finished_at': now()})
        raise HTTPException(409, str(error))
    response = JSONResponse({'run': run_id})
    response.set_cookie(backup_cookie(config), token, httponly=True, secure=request.url.scheme == 'https', samesite='strict', path='/')
    return response


def read_run_safely(run_id):
    from .admin_runs import read_run
    try:
        return read_run(run_id)
    except (OSError, ValueError):
        return {'id': run_id, 'started_at': now()}


def captured_roots(config):
    media_root = (config.get('destination') or {}).get('root')
    return [root for root in (configuration_root(), DATA, media_root) if root]


def restorable_backups():
    return [{'directory': run['directory'], 'started_at': run['started_at'], 'media_files': run.get('media_files'),
             'media_bytes': run.get('media_bytes')} for run in all_runs()[:HISTORY]
            if run.get('verified_at')
            and Path(run['directory']).is_dir()]


@router.get('/api/backup/restores')
def restores(request: Request):
    require_admin(request)
    return {'runs': [admin_restore.public_run(run) for run in admin_restore.all_runs()[:HISTORY]],
            'backups': restorable_backups(), 'preferences': read_preferences()}


def restore_plan(config, backup, password, parent):
    """Refuse anything that would touch the library, or could not finish, before doing any work."""
    if not isinstance(backup, str) or not isinstance(password, str) or not isinstance(parent, str):
        raise HTTPException(422, 'A backup, its administrator password and a folder are required')
    if not password:
        raise HTTPException(409, 'Enter the administrator password that was in use when this backup was made')
    backup_folder, folder = Path(os.path.abspath(backup.strip())), Path(os.path.abspath(parent.strip()))
    if not parent.strip() or not folder.is_dir():
        raise HTTPException(409, 'Choose an existing folder for the restored copy')
    if not backup.strip() or not (backup_folder / 'library/complete.fernet').is_file():
        raise HTTPException(409, 'That folder is not a finished backup')
    roots = captured_roots(config)
    if any(inside(folder, root) or inside(root, folder) for root in roots):
        raise HTTPException(409, 'The restored copy must be outside the library, data and configuration folders')
    if inside(folder, backup_folder) or inside(backup_folder, folder):
        raise HTTPException(409, 'The restored copy must be outside the backup folder')
    try:
        check_destination_privacy(folder)
    except UnprotectedDestination:
        raise HTTPException(409, 'A restored copy holds private settings, so it needs a disk that can keep files private.')
    except OSError as error:
        raise HTTPException(409, f'That folder cannot be written: {error.strerror or error}')
    pg_dump = find_pg_dump(config)
    tools, missing = admin_restore.tools_for(pg_dump) if pg_dump else (None, ['pg_dump'])
    if not tools:
        raise HTTPException(409, f"The PostgreSQL tools are incomplete: {', '.join(missing)} not found")
    with connection() as db:
        media_bytes, database_bytes = sizes(db)
    needed = admin_restore.space_needed(media_bytes, database_bytes)
    if shutil.disk_usage(folder).free < needed:
        raise HTTPException(409, 'That disk does not have enough free space for a restored copy.')
    return {'backup_directory': str(backup_folder), 'tools': str(tools),
            'database_budget': database_bytes * 2 + DUMP_ALLOWANCE}, folder


@router.post('/api/backup/restore')
def start_restore(request: Request, payload: dict = Body(...)):
    """The application keeps running: this only reads the backup and writes a new folder."""
    require_admin(request)
    if not starting.acquire(blocking=False):
        raise HTTPException(409, 'A backup or restore is already starting')
    try:
        if admin_restore.current_run():
            raise HTTPException(409, 'A restore test is already running')
        if current_run():
            raise HTTPException(409, 'A backup is running')
        plan_values, parent = restore_plan(load_config(), payload.get('backup_directory'), payload.get('password'),
                                           payload.get('parent'))
        run_id = admin_restore.new_run_id()
        folder = admin_restore.create_folder(parent, run_id)
        admin_restore.write_run({'id': run_id, 'state': 'starting', 'started_at': now(), 'directory': str(folder), **plan_values})
        launch_restore(run_id, payload['password'])
        return {'run': run_id, 'directory': str(folder)}
    finally:
        starting.release()


def launch_restore(run_id, password):
    """A wrong password ends the run as failed before the worker writes anything."""
    try:
        admin_restore.launch(run_id, ROOT, password)
    except HandoffFailed as error:
        admin_restore.write_run({**admin_restore.read_run(run_id), 'state': 'failed', 'error': str(error), 'failed_at': now()})
        raise HTTPException(409, str(error))


@router.post('/api/backup/restore/{run_id}/retry')
def retry_restore(run_id: str, request: Request, payload: dict = Body(default={})):
    """A new worker asks for the password again: none is kept between attempts."""
    require_admin(request)
    password = payload.get('password')
    if not isinstance(password, str) or not password:
        raise HTTPException(409, 'Enter the administrator password that was in use when this backup was made')
    if not starting.acquire(blocking=False):
        raise HTTPException(409, 'A backup or restore is already starting')
    try:
        if admin_restore.current_run() or current_run():
            raise HTTPException(409, 'A backup or restore is already running')
        try:
            run = admin_restore.read_run(run_id)
        except (ValueError, OSError):
            raise HTTPException(404, 'Restore not found')
        if run['state'] != 'failed':
            raise HTTPException(409, 'Only a failed restore can be retried')
        admin_restore.write_run({**{key: value for key, value in run.items() if key != 'runner_pid'},
                                 'state': 'starting', 'started_at': now(), 'error': None})
        launch_restore(run_id, password)
        return {'run': run_id}
    finally:
        starting.release()


@router.get('/api/backup-run')
def run_status(request: Request):
    require_admin(request)
    run = current_run() or next(iter(all_runs()), None)
    if not run:
        return {'state': 'none', 'authorized': False, 'finished': True}
    return public_run(run, authorized=token_matches(request.cookies.get(backup_cookie(load_config()), ''), run))


@router.get('/api/backup-run/log')
def run_log(request: Request, after: int = 0):
    """The latest run's log, after FamilyFoto is back; the runner serves it while it is away."""
    require_admin(request)
    run = current_run() or next(iter(all_runs()), None)
    if not run or not token_matches(request.cookies.get(backup_cookie(load_config()), ''), run):
        raise HTTPException(403, 'Only the browser that started this backup can see its log')
    try:
        return read_activity(run['id'], after)
    except ValueError as error:
        raise HTTPException(422, str(error))


@router.get('/backup-progress')
def progress_page(request: Request):
    require_admin(request)
    return FileResponse(ROOT / 'web/backup-progress.html')
