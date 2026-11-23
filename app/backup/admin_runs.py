"""Records and checks for backups an administrator starts from the application.

A capture needs the application stopped, so it cannot run inside the server. The server
only checks a plan and starts `admin_runner`, which reports here. Records live in the log
folder, outside the captured roots, and hold no secrets: the administrator password that
protects a backup goes to the runner on a private pipe only, and a run keeps only a digest
of the token that lets one browser follow it.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import unicodedata
import uuid

from ..config import configuration_root, DATA, LOGS, ROOT
from .media_copy import sync_directory
from .policy import check_directory, classify_file, classify_table, schema_kind, UnclassifiedState
from .private_storage import check_destination_privacy, UnprotectedDestination
from .recovery import save_private
from .state_files import state_inventory


COOKIE = 'familyfoto_backup_run'
RUNS = LOGS / 'backup-runs'
PREFERENCES = RUNS / 'preferences.json'
RUN_ID = re.compile(r'[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}')
ACTIVE = {'starting', 'stopping-app', 'capturing', 'verifying', 'failed', 'restarting'}
RECOVERED_SERVER = 'app.migration.launch'
SUPPORTED_SERVERS = {'app.service.desktop', 'app.service.live', RECOVERED_SERVER}
# On a Mac the service's own supervisor stops only the web server on request; see app.service.macos_lifecycle.
MACOS_SERVER = 'app.service.macos'
# A database dump can exceed the size reported for the database only slightly; allow for it.
DUMP_ALLOWANCE = 64 * 1024 ** 2
WRITE_ATTEMPTS = 50
# How long a new run may take to report which process is running it.
RUNNER_START_SECONDS = 120
UNCLASSIFIED_SHOWN = 10


def now():
    return datetime.now(timezone.utc).isoformat()


def new_run_id():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]


def run_path(run_id):
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise ValueError('Invalid backup run')
    return RUNS / f'{run_id}.json'


def write_run(run):
    RUNS.mkdir(parents=True, exist_ok=True)
    # Windows refuses to replace a file another process is reading at that moment, which
    # must never be what ends a backup. A brief retry is enough for a status read to finish.
    for attempt in range(WRITE_ATTEMPTS):
        try:
            save_private(run_path(run['id']), {**run, 'updated_at': now()})
            break
        except PermissionError:
            if attempt == WRITE_ATTEMPTS - 1:
                raise
            time.sleep(0.1)
    sync_directory(RUNS)
    return run


def read_run(run_id):
    return json.loads(run_path(run_id).read_text(encoding='utf-8'))


def all_runs():
    if not RUNS.is_dir():
        return []
    runs = []
    for path in sorted(RUNS.glob('*.json'), reverse=True):
        if RUN_ID.fullmatch(path.stem):
            try:
                runs.append(json.loads(path.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                continue
    return runs


def process_alive(pid):
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.OpenProcess.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32)
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: it exists, but isn't ours to read.
        code = ctypes.c_uint32()
        kernel.GetExitCodeProcess.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32))
        alive = kernel.GetExitCodeProcess(handle, ctypes.byref(code)) and code.value == 259  # STILL_ACTIVE
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle(handle)
        return bool(alive)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # It exists, but isn't ours to signal.
    except OSError:
        return False
    return True


def runner_gone(run):
    """Nothing is working on this record any more, so it can't still be running."""
    pid = run.get('runner_pid')
    if pid is not None:
        return not process_alive(pid)
    started = datetime.fromisoformat(run['started_at'])
    return (datetime.now(timezone.utc) - started).total_seconds() > RUNNER_START_SECONDS


def current_run():
    """The newest run still being worked on. Only the running server calls this.

    With the server up no gate is held, so a run whose runner has gone is just over, and
    must not block every later backup until someone clears its record by hand.
    """
    for run in all_runs():
        if run.get('state') not in ACTIVE:
            continue
        if not runner_gone(run):
            return run
        write_run({**run, 'state': 'finished', 'gave_up': True, 'finished_at': now(),
                   'error': run.get('error') or 'The backup process stopped before it finished.'})
    return None


def token_digest(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def token_matches(cookie, run):
    return bool(cookie) and hmac.compare_digest(token_digest(cookie), run['token_sha256'])


def public_run(run, *, authorized):
    """A browser without this run's token only learns that a backup is running."""
    if not authorized:
        return {'authorized': False, 'state': run['state'], 'finished': run['state'] == 'finished'}
    hidden = {'token_sha256', 'restart'}
    return {**{key: value for key, value in run.items() if key not in hidden}, 'authorized': True,
            'finished': run['state'] == 'finished'}


def read_preferences():
    try:
        value = json.loads(PREFERENCES.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {'destination': ''}
    return {'destination': str(value.get('destination', ''))}


def save_preferences(destination):
    # Written whole, so an older recovery-key path is dropped, never read.
    RUNS.mkdir(parents=True, exist_ok=True)
    save_private(PREFERENCES, {'destination': destination})


def find_pg_dump(config):
    """The configured pg_dump, then the application's own runtime, then the system's."""
    name = 'pg_dump.exe' if os.name == 'nt' else 'pg_dump'
    configured = (config.get('backup') or {}).get('pg_dump')
    candidates = [Path(configured)] if configured else []
    candidates += [ROOT / 'runtime/postgres/bin' / name, DATA.parent / 'runtime/postgres/bin' / name]
    candidates += homebrew_postgres_tools(name)
    found = next((path for path in candidates if path.is_file()), None)
    if found:
        return found
    on_path = shutil.which('pg_dump')
    return Path(on_path) if on_path else None


def homebrew_postgres_tools(name):
    """The Mac installer records where its PostgreSQL tools are. The service's own PATH is too short to find them."""
    try:
        receipt = json.loads((ROOT / 'runtime/macos-preparation.json').read_text(encoding='utf-8'))
        return [Path(receipt['tools']['postgres']).parent / name]
    except (OSError, ValueError, KeyError, TypeError):
        return []


def inside(path, root):
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    return path == root or path.is_relative_to(root)


def check_plan(config, destination, password, *, media_bytes, database_bytes):
    """Everything that can be refused while the application is still running and usable."""
    problems = []
    report = {'media_bytes': media_bytes, 'database_bytes': database_bytes, 'problems': problems,
              'unprotected': False, 'free_bytes': None, 'needed_bytes': None}
    media_root = (config.get('destination') or {}).get('root')
    if not media_root or not Path(media_root).is_dir():
        problems.append('The library folder is not available.')
    if not find_pg_dump(config):
        problems.append('pg_dump was not found. Set backup.pg_dump in the configuration file.')
    roots = [root for root in (configuration_root(), DATA, media_root) if root]
    folder = Path(os.path.abspath(destination)) if destination else None
    if folder is None or not folder.is_absolute() or not folder.is_dir():
        problems.append('Choose an existing folder for the backup.')
    elif any(inside(folder, root) or inside(root, folder) for root in roots):
        problems.append('The backup must be outside the library, data and configuration folders.')
    else:
        try:
            check_destination_privacy(folder)
        except UnprotectedDestination:
            report['unprotected'] = True
        except OSError as error:
            problems.append(f'The backup folder cannot be written: {error.strerror or error}')
        try:
            report['free_bytes'] = shutil.disk_usage(folder).free
        except OSError:
            problems.append('The free space on the backup disk cannot be read.')
        # Rough but honest: the capture checks again, against what it actually finds.
        report['needed_bytes'] = media_bytes + database_bytes * 2 + DUMP_ALLOWANCE
        if report['free_bytes'] is not None and report['free_bytes'] < report['needed_bytes']:
            problems.append('The backup disk does not have enough free space. '
                            f"Needed about {report['needed_bytes'] / 1024**3:.2f} GiB "
                            f"({report['needed_bytes']:,} bytes); free {report['free_bytes'] / 1024**3:.2f} GiB "
                            f"({report['free_bytes']:,} bytes).")
    problems.extend(check_password(config, password))
    if problems:
        return report
    roots = {'config': configuration_root(), 'data': DATA}
    found = {}

    def note_unknown(kind, relative):
        parts = relative.split('/')
        name = '/'.join([kind, *parts[:2 if len(parts) > 2 else 1]])
        found[name] = found.get(name, 0) + 1

    try:
        state_inventory(roots, on_unclassified=note_unknown)
    except (OSError, ValueError) as error:
        problems.append(f'The application state cannot be backed up yet: {error}')
    unknown = sorted(found.items(), key=lambda item: (-item[1], item[0]))
    if unknown:
        problems.extend(f'A backup has no rule yet for {name} ({count} files). It will not guess whether to keep it.'
                        for name, count in unknown[:UNCLASSIFIED_SHOWN])
        if len(unknown) > UNCLASSIFIED_SHOWN:
            problems.append(f'And {len(unknown) - UNCLASSIFIED_SHOWN} more folders like these.')
    return report


# The same schemas and relations app.backup.inventory looks at: everything but PostgreSQL's own.
SCHEMAS = ("SELECT nspname AS name FROM pg_catalog.pg_namespace WHERE NOT starts_with(nspname, 'pg_') "
           "AND nspname <> 'information_schema' ORDER BY nspname")
RELATIONS = ("SELECT n.nspname AS schema, c.relname AS name, c.relkind AS kind FROM pg_catalog.pg_class c "
             "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
             "WHERE NOT starts_with(n.nspname, 'pg_') AND n.nspname <> 'information_schema' "
             "AND c.relkind IN ('r', 'p', 'v', 'm', 'f') ORDER BY 1, 2")


def unclassified_database(db):
    """What the capture's database inventory would refuse, all at once, while the app still runs.

    The same queries and rules as app.backup.inventory, which stops at the first one. Found
    here, a stray schema is reported on the page, not after FamilyFoto has been stopped.
    """
    found, bad = [], set()
    for row in db.execute(SCHEMAS).fetchall():
        try:
            schema_kind(row['name'])
        except UnclassifiedState:
            bad.add(row['name'])
            found.append(f'The database schema {row["name"]} has no backup rule. It may be left by tests; '
                         'remove it, or ask for a rule, before backing up.')
    for row in db.execute(RELATIONS).fetchall():
        if row['schema'] in bad:
            continue
        try:
            if row['kind'] not in ('r', 'p'):
                raise UnclassifiedState(row['name'])
            classify_table(row['schema'], row['name'])
        except UnclassifiedState:
            found.append(f'The database table {row["schema"]}.{row["name"]} has no backup rule.')
    db.commit()
    return found


def estimate_seconds(runs, media_bytes):
    """From the last verified backup, scaled by size. None before the first one.

    One-pass runs come first: an older three-pass run reads the library more times, so it would
    overstate the next run. It is used only while no one-pass run has been verified.
    """
    runs = list(runs)
    for run in [run for run in runs if run.get('single_pass') is True] + [run for run in runs if run.get('single_pass') is not True]:
        if run.get('verified_at') and run.get('media_bytes'):
            started = datetime.fromisoformat(run['started_at'])
            taken = (datetime.fromisoformat(run['verified_at']) - started).total_seconds()
            if taken > 0:
                return {'seconds': round(taken * max(media_bytes, 1) / run['media_bytes']), 'last_seconds': round(taken),
                        'from_started_at': run['started_at'], 'from_media_bytes': run['media_bytes']}
    return None


def unclassified_state(roots):
    """Everything a capture would refuse, by folder, so all of it can be seen at once."""
    found = {}

    def note(kind, relative):
        parts = relative.split('/')
        name = '/'.join([kind, *parts[:2 if len(parts) > 2 else 1]])
        found[name] = found.get(name, 0) + 1

    for kind, root in roots.items():
        for base, folders, names in os.walk(root):
            relative = Path(base).relative_to(root).as_posix()
            if relative != '.':
                try:
                    check_directory(kind, relative)
                except UnclassifiedState:
                    note(kind, relative)
            for file in names:
                path = file if relative == '.' else f'{relative}/{file}'
                try:
                    classify_file(kind, path)
                except UnclassifiedState:
                    note(kind, path)
    return sorted(found.items(), key=lambda item: (-item[1], item[0]))


def check_password(config, password):
    """The backup password is the administrator password, as it is now: check it against the
    account's stored hash, and that it is long enough to protect a backup."""
    from . import passwords
    if not password:
        return ['Enter your FamilyFoto administrator password. It protects the backup.']
    encoded = config.get('browser_password_hash')
    try:
        passwords.BackupPassword(password)
    except passwords.PasswordProblem as error:
        return [str(error)]
    if not isinstance(encoded, str) or not passwords.accepts(encoded, password):
        return ['This is not the FamilyFoto administrator password.']
    if len(unicodedata.normalize('NFC', password)) < passwords.MINIMUM_CHARACTERS:
        return ['Your administrator password is too short to protect a backup. Change it to at least '
                f'{passwords.MINIMUM_CHARACTERS} characters in Settings, then back up.']
    return []


def detached_options():
    if os.name == 'nt':
        # Not DETACHED_PROCESS: a virtual environment's python.exe starts the real interpreter
        # as its child, and a child of a process with no console opens a new console window.
        # That window meant nothing to the administrator, and closing it stopped the process
        # (for a restarted server, FamilyFoto). CREATE_NO_WINDOW gives both a hidden console.
        return {'creationflags': subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_BREAKAWAY_FROM_JOB}
    return {'start_new_session': True}


# The run's log, for the administrator who started it: what it is doing, in words, plus any
# warnings. It holds paths and counts, never key material or the run's token.
LOG_READ_BYTES = 64 * 1024


def activity_path(run_id):
    return run_path(run_id).with_suffix('.log')


def note_activity(run_id, message):
    RUNS.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')
    with activity_path(run_id).open('a', encoding='utf-8') as output:
        for line in str(message).splitlines() or ['']:
            output.write(f'{stamp}  {line}\n')


def note_activity_safely(run_id, message):
    """For a caller with nothing else guarding it: the log must never be what stops a backup."""
    try:
        note_activity(run_id, message)
    except OSError:
        pass


def read_activity(run_id, after):
    """The log from byte `after` on, at most LOG_READ_BYTES, in whole lines only.

    A line being written as it is read is left for the next read: half of it could end in half of
    a character, such as one in a Chinese folder name. Only a single line longer than a whole
    read is returned in pieces, since it could never be read whole.
    """
    if type(after) is not int or after < 0:
        raise ValueError('Invalid log position')
    try:
        with activity_path(run_id).open('rb') as source:
            source.seek(after)
            chunk = source.read(LOG_READ_BYTES)
    except FileNotFoundError:
        return {'text': '', 'next': 0}
    if b'\n' in chunk:
        chunk = chunk[:chunk.rindex(b'\n') + 1]
    elif len(chunk) < LOG_READ_BYTES:
        chunk = b''
    return {'text': chunk.decode('utf-8', errors='replace'), 'next': after + len(chunk)}


def backup_directory(destination, run_id):
    return Path(os.path.abspath(destination)) / f'familyfoto-backup-{run_id}'
