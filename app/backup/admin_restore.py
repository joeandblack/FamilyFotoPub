"""Prove a backup restores, in its own process, without touching the running library.

It works only inside a new private folder: the restored library, and a temporary PostgreSQL
database cluster that listens on this computer alone, needs a password, and is stopped
afterwards. The restored copy stays validation-only. Nothing here activates it, starts the
application from it or changes the source. It holds no application lease, so a backup can
still run, and it never loads the application's settings.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

from ..config import LOGS
from .admin_runs import detached_options, now, process_alive, RUNNER_START_SECONDS
from .encrypted import verify_artifact
from .library import verify_library_preflight
from .library_restore import recovered_media_copy, restore_library
from . import password_handoff, passwords
from .media_copy import sync_directory
from .postgres import Endpoint
from .private_storage import create_private_directory
from .recovery import save_private
from .space import SpaceGuard


RESTORES = LOGS / 'backup-restores'
RUN_ID = re.compile(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}')
ACTIVE = {'starting', 'checking-backup', 'preparing-database', 'restoring', 'validating', 'stopping-database'}
ADMIN = 'familyfoto_recovery_admin'
# Enough for a large dump to be finished and indexed; the disk check is what really limits it.
START_TIMEOUT = 120
PROGRESS_SECONDS = 1
GENERIC_LINE = re.compile(r'could not start server|Examine the log output|stopped waiting|waiting for server')
REASON_LINE = re.compile(r'FATAL|PANIC|ERROR|could not|lock file|another server|Permission denied')


def new_run_id():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]


def run_path(run_id):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError('Invalid restore run')
    return RESTORES / f'{run_id}.json'


def write_run(run):
    RESTORES.mkdir(parents=True, exist_ok=True)
    for attempt in range(50):
        try:
            save_private(run_path(run['id']), {**run, 'updated_at': now()})
            break
        except PermissionError:
            # Windows refuses to replace a file while the page is reading it.
            if attempt == 49:
                raise
            time.sleep(0.1)
    sync_directory(RESTORES)
    return run


def read_run(run_id):
    return json.loads(run_path(run_id).read_text(encoding='utf-8'))


def runner_gone(run):
    pid = run.get('runner_pid')
    if pid is not None:
        return not process_alive(pid)
    started = datetime.fromisoformat(run['started_at'])
    return (datetime.now(timezone.utc) - started).total_seconds() > RUNNER_START_SECONDS


def all_runs():
    """Newest first. A record with nobody working on it is reported as the failure it is."""
    if not RESTORES.is_dir():
        return []
    runs = []
    for path in sorted(RESTORES.glob('*.json'), reverse=True):
        if not RUN_ID.fullmatch(path.stem):
            continue
        try:
            run = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if run.get('state') in ACTIVE and runner_gone(run):
            # Stop first: while this record still counts as running, a retry can't start beside it.
            stop_leftover_database(run)
            run = write_run({**run, 'state': 'failed', 'error': run.get('error') or 'The restore process stopped before it finished.'})
        runs.append(run)
    return runs


def stop_leftover_database(run):
    """A killed runner leaves its server running, with nobody left to stop it."""
    if not run.get('directory') or not run.get('tools'):
        return
    try:
        RecoveryCluster(Path(run['directory']), run['tools'], None).stop()
    except (OSError, subprocess.SubprocessError):
        pass  # A retry checks again, and reports it if the server still can't be stopped.


def current_run():
    return next((run for run in all_runs() if run.get('state') in ACTIVE), None)


def public_run(run):
    return {key: value for key, value in run.items() if key not in {'tools', 'runner_pid'}}


def free_port():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def port_is_free(port):
    with socket.socket() as probe:
        if os.name != 'nt':
            # As PostgreSQL does, so a port the last attempt just closed still counts as free.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            return False
    return True


def discard_unstarted(folder):
    """A step that stopped after making its folder recorded nothing, and can't resume without a record.
    Anything else in there isn't ours to judge, so it stays and the step refuses it."""
    if folder.is_dir() and {path.name for path in folder.iterdir()} <= {'copy.lock'}:
        (folder / 'copy.lock').unlink(missing_ok=True)
        folder.rmdir()


class RecoveryCluster:
    """A temporary database server for this restore only, stopped whatever happens."""

    def __init__(self, directory, tools, environment):
        self.directory, self.tools, self.environment = directory, Path(tools), environment
        self.data = directory / 'database'
        self.password_file = directory / 'database-password.txt'
        self.port = None

    def run(self, name, *arguments, timeout=300):
        """Output goes to a file. The server pg_ctl starts inherits it, and a pipe would never close."""
        log = self.directory / 'database-tools.log'
        offset = size_of(log)
        with log.open('ab') as output:
            code = subprocess.run([str(self.tools / name), *arguments], stdout=output, stderr=output, timeout=timeout,
                                  stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).returncode
        return SimpleNamespace(returncode=code, output=text_after(log, offset))

    def create(self):
        """Same locale settings as the source, because recovery refuses a different one."""
        descriptor = os.open(self.password_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w', encoding='ascii') as output:
            output.write(secrets.token_urlsafe(32))
        locale = self.environment
        arguments = ['-D', str(self.data), '-U', ADMIN, f'--pwfile={self.password_file}', '--auth=scram-sha-256',
                     f"--encoding={locale['encoding']}", f"--lc-collate={locale['collate']}", f"--lc-ctype={locale['ctype']}"]
        result = self.run('initdb' + executable_suffix(), *arguments)
        if result.returncode:
            raise RuntimeError(f'The temporary database could not be created: {last_line(result)}')

    def running(self):
        return self.run('pg_ctl' + executable_suffix(), 'status', '-D', str(self.data)).returncode == 0

    def stop_leftover(self):
        """This folder belongs to this restore alone, so a server on it can only be from an earlier attempt."""
        if self.running():
            self.stop()
            if self.running():
                raise RuntimeError('A temporary database from an earlier attempt is still running and could not be stopped')

    def recorded_port(self):
        """The restore's records name the server they were made on, so a retry needs the same port."""
        record = self.directory / 'library/restore-job.json'
        if not record.exists():
            return None
        try:
            port = json.loads(record.read_text(encoding='utf-8'))['endpoint'][1]
        except (OSError, ValueError, KeyError, IndexError, TypeError) as error:
            raise RuntimeError('The port this restore was using could not be read') from error
        if type(port) is not int or not 1 <= port <= 65535:
            raise RuntimeError('The port this restore recorded is not valid')
        return port

    def start(self):
        if self.data.is_dir():
            self.stop_leftover()
        else:
            self.create()
        recorded = self.recorded_port()
        if recorded is not None and not port_is_free(recorded):
            raise RuntimeError(f'Port {recorded}, which this restore uses for its temporary database, is in use by '
                               'another program. Try again once it is free.')
        self.port = recorded or free_port()
        settings = f"-p {self.port} -h 127.0.0.1"
        server_log = self.directory / 'database.log'
        offset = size_of(server_log)
        result = self.run('pg_ctl' + executable_suffix(), 'start', '-D', str(self.data), '-w', '-t', str(START_TIMEOUT),
                          '-l', str(server_log), '-o', settings, timeout=START_TIMEOUT + 30)
        if result.returncode:
            # The server writes its own reason to its log, not to pg_ctl's output.
            output = result.output + text_after(server_log, offset)
            raise RuntimeError(f'The temporary database could not start: {last_line(SimpleNamespace(returncode=result.returncode, output=output))}')

    def stop(self):
        if (self.data / 'postmaster.pid').exists():
            self.run('pg_ctl' + executable_suffix(), 'stop', '-D', str(self.data), '-m', 'fast', '-w', '-t', str(START_TIMEOUT),
                     timeout=START_TIMEOUT + 30)

    def endpoint(self):
        return Endpoint('127.0.0.1', self.port, 'postgres', ADMIN, self.password_file.read_text(encoding='ascii'))


def executable_suffix():
    return '.exe' if os.name == 'nt' else ''


def size_of(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def text_after(path, offset):
    try:
        with path.open('rb') as source:
            source.seek(offset)
            return source.read().decode('utf-8', errors='replace')
    except FileNotFoundError:
        return ''


def last_line(result):
    """The line that names the reason, not the tool's closing remark about reading a log."""
    lines = [line for line in result.output.strip().splitlines() if line.strip() and not GENERIC_LINE.search(line)]
    named = [line for line in lines if REASON_LINE.search(line)]
    return (named or lines)[-1][:300] if lines else f'exit code {result.returncode}'


class Run:
    def __init__(self, run_id):
        self.value = read_run(run_id)
        self.written = 0

    def update(self, **changes):
        self.value.update(changes)
        write_run(self.value)

    def progress(self, event):
        if not isinstance(event, dict):
            return
        moment = time.monotonic()
        self.value['progress'] = event
        if moment - self.written >= PROGRESS_SECONDS:
            self.written = moment
            write_run(self.value)


def restore(run, key, *, guard_factory=SpaceGuard):
    """Every stage continues from what is already there, so a retry only needs running again.

    `key` was unlocked from the backup's password context when this process started."""
    from ..migration.credentials import prepare_recovery_role
    from ..migration.tls import prepare_recovery_tls
    from ..migration.validation import validate_plain_candidate
    value = run.value
    folder, backup = Path(value['directory']), Path(value['backup_directory'])
    run.update(state='checking-backup', error=None, progress=None)
    # Only the sealed parts here: restore_library rereads all the media first thing, with progress.
    payload = verify_library_preflight(backup, key)
    manifest = verify_artifact(backup / 'catalog/database', key)
    target, role, tls = folder / 'library', folder / 'role', folder / 'tls'
    finished_report = target / 'LIBRARY-RECOVERY-REPORT.json'
    restored_earlier = finished_report.exists()
    if restored_earlier:
        # The report is written last, so a marker that outlived it is all that was left to do.
        (target / 'LIBRARY-RESTORE-INCOMPLETE').unlink(missing_ok=True)
    else:
        discard_unstarted(target / 'catalog')
        discard_unstarted(target)
    discard_unstarted(role)
    cluster = RecoveryCluster(folder, value['tools'], locale_of(manifest))
    run.update(state='preparing-database', capture_id=payload['id'])
    try:
        cluster.start()
        admin = cluster.endpoint()
        run.update(state='restoring')
        pg_restore = Path(value['tools']) / ('pg_restore' + executable_suffix())
        if restored_earlier:
            report = json.loads(finished_report.read_text(encoding='utf-8'))
        else:
            report = restore_library(backup, key, target, admin, pg_restore, expected_cluster_directory=cluster.data,
                                     database_budget=value['database_budget'], resume=target.exists(),
                                     guard_factory=guard_factory, progress=run.progress, validation_follows=True)
        run.update(state='validating', progress=None, media_files=report['media_files'], media_bytes=report['media_bytes'])
        record = json.loads((target / 'catalog/database-target.json').read_text(encoding='utf-8'))
        prepare_recovery_role(admin, record, role, expected_cluster_directory=cluster.data, resume=role.exists())
        if not (tls / 'TLS-REPORT.json').exists():
            # Unfinished certificates can't be told from good ones, and these are cheap to issue again.
            shutil.rmtree(tls, ignore_errors=True)
            prepare_recovery_tls(target / 'catalog/files/config/tls', tls, ['localhost', '127.0.0.1'])
        validated = validate_plain_candidate(backup, key, target, admin, role, tls, expected_cluster_directory=cluster.data,
                                             progress=run.progress, fresh_restore=not restored_earlier)
        if validated['activation_allowed'] is not False or validated['services_started'] is not False:
            raise RuntimeError('The restored copy is not isolated')
        run.update(state='stopping-database', validated_at=now(), restored_library=str(recovered_media_copy(target) / 'media'))
    finally:
        cluster.stop()
    run.update(state='finished', finished_at=now())


def locale_of(manifest):
    environment = manifest['metadata']['compatibility']['environment']
    return {'encoding': encoding_name(environment['encoding']), 'collate': environment['datcollate'],
            'ctype': environment['datctype']}


def encoding_name(value):
    # PostgreSQL reports its encodings by number here. Recovery compares them, so only name what is checked.
    return {6: 'UTF8'}.get(value, value)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-id', required=True)
    parser.add_argument(password_handoff.FLAG, action='store_true', dest='password_on_stdin', required=True)
    arguments = parser.parse_args(argv)
    run = Run(arguments.run_id)
    # The administrator password from when the backup was made opens it, or nothing is written.
    try:
        key = passwords.unlock_context(run.value['backup_directory'],
                                       passwords.BackupPassword(password_handoff.receive()))
    except passwords.PasswordProblem as error:
        password_handoff.refuse(error)
        return 1
    password_handoff.ready()
    run.update(runner_pid=os.getpid())
    try:
        restore(run, key)
    except Exception as error:
        run.update(state='failed', error=str(error)[:500], failed_at=now())
        return 1
    return 0


def launch(run_id, root, password):
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / 'backup-restore.log').open('ab') as output:
        password_handoff.launch([sys.executable, '-m', 'app.backup.admin_restore', '--run-id', run_id], password,
                                cwd=root, log=output, **detached_options())


def create_folder(parent, run_id):
    return create_private_directory(Path(os.path.abspath(parent)) / f'familyfoto-restore-{run_id}')


def tools_for(pg_dump):
    """The server tools beside the pg_dump the backup used, so the versions agree."""
    tools = Path(pg_dump).parent
    missing = [name for name in ('initdb', 'pg_ctl', 'pg_restore') if not (tools / (name + executable_suffix())).is_file()]
    return (tools, []) if not missing else (None, missing)


def space_needed(media_bytes, database_bytes):
    return media_bytes + database_bytes * 3 + 256 * 1024 ** 2


if __name__ == '__main__':
    sys.exit(main())
