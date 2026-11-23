"""Run one administrator-started backup, in its own process, while the application is stopped.

It stops only the server that started it, captures and verifies, and serves a small progress
page on the application's address. A successful run restarts the application. A failed one
waits for the administrator's decision, because the source stays gated until then, so nothing
here retries, acknowledges or restarts on its own. It never holds an application lease, loads
the application's cached settings or connects to the database outside the capture itself.
"""
import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
import uvicorn

from ..config import configuration_root, DATA, ROOT
from ..service.macos_lifecycle import PAUSE_WEB
from ..service.instance import backup_cookie
from ..service.server import server_options
from .admin_runs import (COOKIE, detached_options, note_activity, note_activity_safely, now, process_alive, public_run, read_activity, read_run,
                         token_matches, write_run)
from .admission import acknowledge_failed_maintenance
from .library import capture_library, verify_library, verify_library_thumbnails
from . import password_handoff, passwords
from .postgres import Endpoint
from .recovery import save_private
from .capture_options import capture_strategy


STOP_TIMEOUT = 120
STATIC = {'backup-progress.mjs', 'styles.css', 'daisyui-5.7.50.css', 'familyfoto.ico'}
# Writing every file's progress to disk would slow a capture for no one's benefit.
PROGRESS_SECONDS = 1
PAGE_TIMEOUT = 30
# A Mac path kept as text, so the tests that pretend to be a Mac also pass on Windows.
CAFFEINATE = '/usr/bin/caffeinate'
# No idle sleep, no system sleep on mains power, no disk idle sleep (the backup disk). Not the display.
AWAKE = ('-i', '-s', '-m')
# How often the log says how far a long step has got.
LOG_PROGRESS_SECONDS = 30
STATE_NOTES = {
    'stopping-app': 'Stopping FamilyFoto, so nothing changes while it is copied.',
    'capturing': 'Backing up: saving the catalog and application data, then copying the photos and videos.',
    'verifying': 'Checking the finished backup using its recorded verification mode.',
    'failed': 'The backup stopped. FamilyFoto stays stopped until you choose on the progress page.',
    'restarting': 'Starting FamilyFoto again.',
    'finished': 'Finished.',
}
PHASES = {'inventory': 'Reading the library', 'inventory-verify': 'Rechecking the library', 'database': 'Saving the catalog',
          'state': 'Saving application data', 'copy': 'Copying photos and videos', 'verify': 'Checking the copies',
          'thumbnails-inventory': 'Finding thumbnails', 'thumbnails': 'Saving thumbnails',
          'thumbnails-verify': 'Checking saved thumbnails', 'copy-source-check': 'Checking media before copying',
          'copy-paths': 'Checking destination paths', 'copy-inventory-check': 'Checking the copy inventory',
          'copy-space': 'Checking remaining copy space', 'copy-folders': 'Preparing destination folders'}
PHASES.update({'copy-and-verify': 'Copying and checking photos and videos',
               'media-preparation': 'Preparing media copy',
               'media-transfer': 'Copying photos and videos',
               'media-verification': 'Verifying copied media',
               'media-final-check': 'Checking final media consistency',
               'worker-exit': 'Waiting for the media worker to finish shutting down',
               'media-inventory': 'Reading the media inventory', 'catalog': 'Saving the catalog',
               'media-copy': 'Copying media', 'publication': 'Completing the backup'})


def amount(value):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024:
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.1f} {unit}'
        value /= 1024
    return f'{value:.1f} TB'


def total_time_note(run):
    try:
        start = datetime.fromisoformat(run['started_at'])
        finish = datetime.fromisoformat(run['finished_at'])
        if start.utcoffset() is None or finish.utcoffset() is None:
            return None
        elapsed = (finish - start).total_seconds()
        if elapsed < 0:
            return None
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    hours, remaining = divmod(int(elapsed), 3600)
    minutes, seconds = divmod(remaining, 60)
    duration = (f'{hours}h ' if hours else '') + (f'{minutes}m ' if hours or minutes else '') + f'{seconds}s'
    return f'Total elapsed time: {duration} (start to finish, including waits and restart).'


def progress_note(event):
    phase = event.get('phase')
    if phase == 'worker-exited':
        return (f"Media worker exited cleanly: {event['elapsed_seconds']:.3f} seconds after completion, "
                f"exit code {event['exit_code']}; backup finalization remains")
    name = PHASES.get(phase)
    if not name:
        return None
    if phase in {'media-preparation', 'media-transfer', 'media-verification', 'media-final-check'}:
        state = event.get('state', 'running')
        elapsed = event.get('elapsed_seconds', 0)
        counts = (f", {event.get('files', 0)} of {event['total_files']} files, "
                  f"{amount(event.get('bytes', 0))} of {amount(event.get('total_bytes', 0))}") if 'total_files' in event else ''
        return f'{name}: {state}, {elapsed:.3f} seconds{counts}'
    if 'total_files' in event:
        return (f"{name}: {event.get('files', 0)} of {event['total_files']} files, "
                f"{amount(event.get('bytes', 0))} of {amount(event.get('total_bytes', 0))}")
    if 'entries' in event:
        return f"{name}: {event['entries']} entries"
    if 'files' in event:
        return f"{name}: {event['files']} files, {amount(event.get('bytes', 0))}"
    return f"{name}: {amount(event.get('bytes', 0))}" if 'bytes' in event else name


class ActivityHandler(logging.Handler):
    """Warnings and errors logged anywhere in this process go into the run's log too."""

    def __init__(self, run_id):
        super().__init__(logging.WARNING)
        self.run_id = run_id

    def emit(self, record):
        try:
            message = f'{record.levelname.lower()}: {record.getMessage()}'
            if record.exc_info:
                message += '\n' + logging.Formatter().formatException(record.exc_info)
            note_activity(self.run_id, message)
        except Exception:
            pass  # The log must never be what stops a backup.


class Run:
    """The run's record, kept on disk so it survives this process."""

    def __init__(self, run_id):
        self.value = read_run(run_id)
        self.lock = threading.Lock()
        self.decision = threading.Event()
        self.action = None
        self.written = 0
        self.phase = None
        self.noted = 0

    def note(self, message):
        try:
            note_activity(self.value['id'], message)
        except OSError:
            pass  # The log must never be what stops a backup.

    def update(self, **changes):
        state = changes.get('state')
        with self.lock:
            before = self.value.get('state')
            self.value.update(changes)
            write_run(self.value)
        if state and state != before and state in STATE_NOTES:
            self.note(STATE_NOTES[state])
        if changes.get('error'):
            self.note(f"Error: {changes['error']}")
        if state == 'finished' and before != 'finished':
            total = total_time_note(self.value)
            if total:
                self.note(total)

    def progress(self, event):
        if not isinstance(event, dict):
            return
        text = progress_note(event)
        boundary = event.get('phase') in {'media-preparation', 'media-transfer', 'media-verification', 'media-final-check'} and event.get('state') in {'started', 'completed', 'failed'}
        if text and (boundary or event.get('phase') != self.phase or time.monotonic() - self.noted >= LOG_PROGRESS_SECONDS):
            self.phase, self.noted = event.get('phase'), time.monotonic()
            self.note(text)
        moment = time.monotonic()
        if moment - self.written < PROGRESS_SECONDS:
            with self.lock:
                self.value['progress'] = event
            return
        self.written = moment
        self.update(progress=event)


def progress_connection_reset(context):
    """Only the Windows socket-shutdown callback, never copy/verification failures."""
    error = context.get('exception')
    callback = getattr(context.get('handle'), '_callback', None)
    return (isinstance(error, ConnectionResetError)
            and (getattr(error, 'winerror', None) or error.errno) == 10054
            and getattr(callback, '__module__', None) == 'asyncio.proactor_events'
            and getattr(callback, '__qualname__', None) == '_ProactorBasePipeTransport._call_connection_lost')


def status_app(run, cookie=COOKIE):
    """`cookie` is this installation's backup cookie (app.service.instance.backup_cookie)."""
    @asynccontextmanager
    async def lifespan(app):
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        reported = False

        def connection_error(loop, context):
            nonlocal reported
            if progress_connection_reset(context):
                if not reported:
                    reported = True
                    run.note('A progress-page connection was reset by its client. Backup work continues independently.')
                return
            if previous:
                previous(loop, context)
            else:
                loop.default_exception_handler(context)

        loop.set_exception_handler(connection_error)
        try:
            yield
        finally:
            loop.set_exception_handler(previous)

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def authorized(request):
        return token_matches(request.cookies.get(cookie, ''), run.value)

    @app.get('/api/backup-run')
    def status(request: Request):
        return JSONResponse(public_run(run.value, authorized=authorized(request)),
                            headers={'Cache-Control': 'no-store'})

    @app.post('/api/backup-run/decision')
    async def decide(request: Request):
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'error': 'Cross-origin commands are not allowed'}, 403)
        if not authorized(request):
            return JSONResponse({'error': 'Only the browser that started this backup can decide'}, 403)
        action = (await request.json()).get('action')
        if action not in {'retry', 'restart'} or run.value['state'] != 'failed' or run.decision.is_set():
            return JSONResponse({'error': 'This backup is not waiting for a decision'}, 409)
        run.action = action
        run.decision.set()
        run.note('You chose to try again.' if action == 'retry' else 'You chose to give up and start FamilyFoto again.')
        return {'accepted': action}

    @app.get('/api/backup-run/log')
    def log(request: Request, after: int = 0):
        if not authorized(request):
            return JSONResponse({'error': 'Only the browser that started this backup can see its log'}, 403)
        try:
            return JSONResponse(read_activity(run.value['id'], after), headers={'Cache-Control': 'no-store'})
        except ValueError:
            return JSONResponse({'error': 'Invalid log position'}, 422)

    @app.get('/web/{name}')
    def asset(name: str):
        if name not in STATIC:
            return Response(status_code=404)
        return FileResponse(ROOT / 'web' / name)

    @app.api_route('/api/{anything:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'])
    def unavailable_api(anything: str):
        return JSONResponse({'error': 'FamilyFoto is stopped while the backup runs.'}, 503,
                            headers={'Cache-Control': 'no-store'})

    @app.get('/{anything:path}')
    def page(anything: str):
        return FileResponse(ROOT / 'web/backup-progress.html', headers={'Cache-Control': 'no-store'})

    return app


class StatusServer:
    def __init__(self, run, config):
        options = {**server_options(config, renew=False), 'log_level': 'warning'}
        self.server = uvicorn.Server(uvicorn.Config(status_app(run, backup_cookie(config)), **options))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self):
        self.thread.start()
        deadline = time.monotonic() + PAGE_TIMEOUT
        while not self.server.started:
            if not self.thread.is_alive() or time.monotonic() > deadline:
                raise RuntimeError('The backup progress page could not start')
            time.sleep(0.1)

    def stop(self):
        self.server.should_exit = True
        self.thread.join(timeout=30)


def stop_application(run):
    """Ask only the server that started this run to stop, and wait for it to go."""
    run.update(state='stopping-app')
    mac = run.value['restart'].get('macos')
    if mac:
        # The service's supervisor stops the web server and leaves the database up for the capture.
        save_private(PAUSE_WEB, {'run': run.value['id'], 'pid': os.getpid()})
        release = lambda: PAUSE_WEB.unlink(missing_ok=True)  # noqa: E731
    else:
        (DATA / 'DESKTOP-SERVER-STOP').write_text('Backup requested by an administrator', encoding='utf-8')
        release = lambda: (DATA / 'DESKTOP-SERVER-STOP').unlink(missing_ok=True)  # noqa: E731
    deadline = time.monotonic() + STOP_TIMEOUT
    while process_alive(run.value['server_pid']):
        if time.monotonic() > deadline:
            release()
            raise RuntimeError('The application did not stop in time, so the backup did not start')
        time.sleep(0.5)
    if not mac:
        release()
    run.note('FamilyFoto has stopped.')


def read_config():
    # Not load_config: that takes an application lease, which would block the capture.
    return json.loads((configuration_root() / 'local.json').read_text(encoding='utf-8'))


def capture(run, config, key):
    """`key` was made from the administrator password when this process started, and is kept in
    memory for this run's retries only. It is never written down."""
    value = run.value
    # Runs recorded before method selection existed used the resumable engine.
    strategy = capture_strategy(value, default='standard')
    database = config['database']
    endpoint = Endpoint(database['host'], database['port'], database['dbname'], database['user'], database.get('password'))
    directory = Path(value['directory'])
    if key is None:
        raise RuntimeError('This backup process has no administrator password, so it cannot continue. '
                           'Start a new backup from Settings.')
    if strategy == 'standard' and (directory / 'library/complete.fernet').is_file():
        # The capture finished and only its check failed, so a retry checks again. Capturing
        # again would be refused, and the sealed backup may well be fine.
        run.update(state='verifying', error=None, progress=None)
        payload = verify_library(directory, key, run.progress)
        verify_library_thumbnails(directory, key, payload)
        run.update(capture_id=payload['id'], media_files=payload['media_files'],
                   media_bytes=payload['media_bytes'], verified_at=now())
        run.note('The check passed: the backup matches the library.')
        return
    if strategy == 'fast-full':
        from fastbackup.capture import capture_library as capture_fast
        if os.path.lexists(directory):
            retained = [*value.get('retained_attempts', []), str(directory)]
            directory = Path(value['destination']) / f"familyfoto-fastbackup-{value['id']}-{uuid.uuid4().hex}"
            run.update(directory=str(directory), retained_attempts=retained, capture_id=None,
                       media_files=None, media_bytes=None, verified_at=None, assurance=None)
            run.note('Starting a new fast-copy attempt; the previous folder is retained unchanged.')
        run.update(state='capturing', error=None, resumed=False)
        payload = capture_fast(endpoint, value['pg_dump'], configuration_root(), DATA, config['destination']['root'],
            directory, key, database_budget=value['database_budget'], offline_confirmed=True,
            progress=run.progress, allow_unprotected=value['allow_unprotected'])
        # The standalone full-only engine already hashed every destination file.
        # Authenticate the sealed catalog/index and thumbnails without a second media pass.
        from .library import verify_library_metadata
        if verify_library_metadata(directory, key) != payload:
            raise RuntimeError('The finished backup does not match what was captured')
        verify_library_thumbnails(directory, key, payload)
        run.update(capture_id=payload['id'], media_files=payload['media_files'],
                   media_bytes=payload['media_bytes'], verified_at=now(), assurance='full')
        run.note('Full-copy capture and verification completed. Originals are unchanged.')
        return
    resume = (directory / 'capture-job.json').is_file()
    run.update(state='capturing', error=None, resumed=resume)
    payload = capture_library(endpoint, value['pg_dump'], configuration_root(), DATA, config['destination']['root'],
                              directory, key, database_budget=value['database_budget'], offline_confirmed=True,
                              progress=run.progress, resume=resume, allow_unprotected=value['allow_unprotected'],
                              single_pass=value.get('single_pass') is True)
    run.note(f"Copied {payload['media_files']} files, {amount(payload['media_bytes'])}, and the catalog.")
    run.update(state='verifying', capture_id=payload['id'], media_files=payload['media_files'],
               media_bytes=payload['media_bytes'], progress=None)
    if verify_library(directory, key, run.progress) != payload:
        raise RuntimeError('The finished backup does not match what was captured')
    verify_library_thumbnails(directory, key, payload)
    run.update(verified_at=now())
    run.note('The check passed: the backup matches the library.')


def acknowledge_gate():
    """Only ever releases a gate this kind of run took, and only on the administrator's word."""
    marker = DATA / '.recovery-admission/maintenance.json'
    if marker.exists():
        record = json.loads(marker.read_text(encoding='utf-8'))
        if record.get('operation') != 'backup':
            raise RuntimeError('Another kind of maintenance holds the gate; review it from the command line')
        acknowledge_failed_maintenance(DATA, record['id'])


def restart_application(run):
    restart = run.value['restart']
    if restart.get('macos'):
        # The supervisor starts the web server again once nothing asks for it to stay down.
        PAUSE_WEB.unlink(missing_ok=True)
    elif restart.get('task'):
        subprocess.run(['powershell', '-NoProfile', '-Command',
                        f"Start-ScheduledTask -TaskName '{restart['task']}'"],
                       check=True, capture_output=True, timeout=60,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    else:
        subprocess.Popen(restart['argv'], cwd=restart['cwd'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, **detached_options())


def run_backup(run_id, key=None):
    run = Run(run_id)
    config = read_config()
    interrupted = run.value['state'] != 'starting'
    run.update(runner_pid=os.getpid())
    if interrupted:
        # Started again after this process died: never carry on, ask.
        run.update(state='failed', error='The backup was interrupted before it finished.')
    else:
        try:
            capture_strategy(run.value, default='standard')
            stop_application(run)
        except Exception as error:
            # Nothing has been gated yet, and the server may still hold the address.
            run.update(state='finished', gave_up=True, error=str(error)[:500], finished_at=now())
            return
    server = StatusServer(run, config)
    server.start()
    run.note('The progress page is now served by the backup process, at the same address.')
    while True:
        if run.value['state'] != 'failed':
            try:
                capture(run, config, key)
                break
            except Exception as error:
                run.update(state='failed', error=str(error)[:500], failed_at=now())
        run.decision.wait()
        run.decision.clear()
        try:
            acknowledge_gate()
        except Exception as error:
            run.update(error=str(error)[:500])
            continue
        if run.action == 'restart':
            run.update(state='restarting', gave_up=True, error=run.value.get('error'))
            break
        run.update(state='capturing', error=None)
    if not run.value.get('gave_up'):
        run.update(state='restarting', completed_at=now())
    # Leave the page up long enough to say so, then free the address for the application.
    time.sleep(3)
    server.stop()
    try:
        restart_application(run)
    except Exception as error:
        run.update(state='finished', finished_at=now(),
                   restart_error=f'The application could not be restarted: {error}'[:500])
        run.note(f'FamilyFoto could not be started again: {error}')
        return
    run.note('FamilyFoto was asked to start. The progress page links back to it once it answers.')
    run.update(state='finished', finished_at=now())


def recover_from_crash(run_id, error):
    """Never leave the application down for no reason. A held gate stays for its owner."""
    run = Run(run_id)
    if (DATA / '.recovery-admission/maintenance.json').exists():
        run.update(state='failed', error=f'The backup process stopped unexpectedly: {error}'[:500])
        return
    (DATA / 'DESKTOP-SERVER-STOP').unlink(missing_ok=True)
    try:
        restart_application(run)
    except Exception as failure:
        run.update(state='finished', gave_up=True, finished_at=now(), error=str(error)[:500],
                   restart_error=f'The application could not be restarted: {failure}'[:500])
        return
    run.update(state='finished', gave_up=True, finished_at=now(), error=str(error)[:500])


class WindowsAwake:
    """Windows' "this computer is busy" request, held by the thread that runs the backup.

    `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` keeps Windows from sleeping
    while this thread lives, and Windows drops it when the process ends, even if it is killed.
    The display may still turn off. Shaped like the Mac's caffeinate process (`poll`,
    `terminate`), so `main` lets either go the same way.
    """
    CONTINUOUS = 0x80000000
    SYSTEM_REQUIRED = 0x00000001

    def __init__(self, kernel32):
        self.kernel32 = kernel32
        self.held = False

    def hold(self):
        self.held = bool(self.kernel32.SetThreadExecutionState(self.CONTINUOUS | self.SYSTEM_REQUIRED))
        return self.held

    def poll(self):
        return None if self.held else 0

    def terminate(self):
        if self.held:
            self.kernel32.SetThreadExecutionState(self.CONTINUOUS)
            self.held = False


def keep_awake():
    """Keep the computer from sleeping until this process ends: a Mac with caffeinate, Windows
    with WindowsAwake.

    A backup takes hours, and idle sleep part way through stalls it with FamilyFoto down.
    caffeinate holds the assertions for as long as this process lives (`-w`), so it also ends
    if this process is killed. The display may still sleep. Without it the backup still runs:
    it must never be what stops one. Call it from the thread that runs the backup: Windows ties
    the request to the calling thread.
    """
    if sys.platform == 'win32':
        try:
            import ctypes
            awake = WindowsAwake(ctypes.windll.kernel32)
            if awake.hold():
                return awake
        except (OSError, AttributeError):
            pass
        print('This computer may sleep during this backup: Windows refused the request to stay awake.',
              file=sys.stderr, flush=True)
        return None
    if sys.platform != 'darwin':
        return None
    try:
        return subprocess.Popen([CAFFEINATE, *AWAKE, '-w', str(os.getpid())], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        print(f'The Mac may sleep during this backup: caffeinate could not start ({type(error).__name__}).',
              file=sys.stderr, flush=True)
        return None


def key_for_run(run_id, password):
    """A new capture gets a new key. A run whose folder already holds a password context (an
    interrupted capture, or a finished one still to be checked) is unlocked from that context, so
    its sealed partial data and its retry record still open. A different password is refused."""
    try:
        directory = Path(read_run(run_id)['directory'])
    except (OSError, ValueError, KeyError):
        return passwords.new_key(password)
    if passwords.has_context(directory):
        return passwords.unlock_context(directory, password)
    return passwords.new_key(password)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument(password_handoff.FLAG, action='store_true', dest='password_on_stdin')
    arguments = parser.parse_args(argv)
    key = None
    if arguments.password_on_stdin:
        # Before anything is stopped: the administrator password, checked against its stored
        # hash, and this backup's key. A refusal is reported to Settings, and nothing changes.
        try:
            text = password_handoff.receive()
            key = key_for_run(arguments.run_id, passwords.administrator_password(configuration_root(), text))
            del text
        except passwords.PasswordProblem as error:
            password_handoff.refuse(error)
            return 1
        password_handoff.ready()
    # Warnings and errors from anything in this process, without raising the root logger's level:
    # libraries' everyday INFO messages stay out of the run's log and backup-runner.log.
    logging.getLogger().addHandler(ActivityHandler(arguments.run_id))
    note_activity_safely(arguments.run_id, f'The backup process started (process {os.getpid()}). It runs without a window.')
    awake = keep_awake()
    if sys.platform in ('darwin', 'win32'):
        computer = 'the Mac' if sys.platform == 'darwin' else 'this computer'
        note_activity_safely(arguments.run_id, f'Keeping {computer} awake until the backup ends; the display may sleep.'
                             if awake is not None else
                             f'{computer[0].upper() + computer[1:]} may sleep during this backup: it could not be kept '
                             'awake. Keep it awake yourself.')
    try:
        run_backup(arguments.run_id, key)
    except Exception as error:
        recover_from_crash(arguments.run_id, error)
        raise
    finally:
        if awake is not None and awake.poll() is None:
            awake.terminate()
    return 0


if __name__ == '__main__':
    sys.exit(main())
