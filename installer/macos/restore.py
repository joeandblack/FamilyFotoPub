"""Restore from a backup into this new Mac installation, in one go (app.migration.move_in).

Run by `install.sh restore` with this installation's own Python, after Core is installed.
Running it again carries on from the stage that stopped.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from installer.macos import manage, prepare  # noqa: E402
from installer.macos.restore_input import needs_backup, consume_password, close_input  # noqa: E402

STAGE_TEXT = {
    'restored': 'Checking the backup and restoring the catalog and the photos and videos',
    'checked': 'Checking the restored library against the backup',
    'switched': 'Switching on the restored library',
    'thumbnails': 'Putting back the thumbnails, posters and face pictures',
    'installed': 'Moving the library into this installation',
    'people': 'Setting up People on this Mac (this can download the face model or its libraries)',
    'started': 'Starting FamilyFoto: its database server and web server',
    'repaired': 'Rebuilding any thumbnail or poster still missing',
}


class Progress:
    """Plain lines for a person at the terminal: each stage, and a count now and then."""

    def __init__(self):
        self.shown = 0

    def __call__(self, event):
        if not isinstance(event, dict):
            return
        if 'stage' in event:
            text = STAGE_TEXT.get(event['stage'], event['stage'])
            print(('… ' if event['state'] == 'running' else '✓ ') + text, flush=True)
            return
        moment = time.monotonic()
        if moment - self.shown < 15:
            return
        self.shown = moment
        parts = [str(event.get('phase', 'working'))]
        for name in ('files', 'done_files', 'bytes', 'done_bytes'):
            if isinstance(event.get(name), int):
                parts.append(f'{name.replace("_", " ")} {event[name]:,}')
        print('    ' + ', '.join(parts), flush=True)


@contextmanager
def database(config, binary):
    """This installation's own database server, for the restore only. The service's lock keeps
    FamilyFoto itself from starting meanwhile."""
    import fcntl
    with (ROOT / 'runtime/macos-service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit('FamilyFoto or another setup step is running in this installation. Stop it first.') from None
        manage.validate_ready(config)
        prepare.require_free_ports((config['database']['port'],))
        with (ROOT / 'logs/macos-postgres.log').open('a') as log:
            process = subprocess.Popen(manage.postgres_args(config, binary), stdout=log, stderr=log,
                                       env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C'})
            try:
                manage.wait_database(config, process)
                yield
            finally:
                from app.database import close_pools
                close_pools()
                # Only the server this started, never one found by port.
                if process.poll() is None:
                    process.send_signal(signal.SIGTERM)
                    process.wait(timeout=120)


def start():
    manage.start(open_browser=not os.environ.get('FAMILYFOTO_SETUP_WINDOW'))


def installation(people_port):
    from app.backup.postgres import Endpoint
    from app.migration.move_in import Installation
    config, binary = manage.settings(restoring=True)
    admin = Endpoint('127.0.0.1', config['database']['port'], 'postgres', 'postgres',
                     password=config['postgres_admin_password'])
    return Installation(root=ROOT, admin=admin, cluster=manage.CLUSTER.resolve(), pg_restore=binary / 'pg_restore',
                        pg_dump=binary / 'pg_dump', web_port=config['port'], people_port=people_port,
                        database=lambda: database(config, binary), start=start, python=manage.PYTHON)


def backup_password(args):
    """The administrator password the backup was made with: a framed pipe, one stdin line, or the terminal."""
    from app.backup import passwords
    if args.backup_password_frame_stdin or args.backup_password_stdin:
        return consume_password(args)
    return passwords.prompt('FamilyFoto administrator password from the old computer (as it was when this backup was made)')


def confirm_switch(automatic, terminal=False):
    from app.migration.move_in import WARNING

    def confirm(summary):
        print()
        print(WARNING)
        if summary.get('media_files') is not None:
            print(f"The restored library holds {summary['media_files']:,} photos and videos, checked against the backup.")
        if automatic:
            print('Confirmed with --old-library-stopped.')
            return True
        if terminal:
            # The password arrived on a private pipe; terminal confirmation uses its own handle.
            try:
                with open('/dev/tty', 'w') as output, open('/dev/tty', 'r') as input:
                    output.write('Type "switch" to switch on this library, or press Return to stop here: ')
                    output.flush()
                    return input.readline().strip().lower() == 'switch'
            except OSError:
                print('No terminal is available. Run again with --old-library-stopped after stopping the old computer.')
                return False
        if not sys.stdin.isatty():
            print('Stopping here: the library is restored and checked, but not switched on. Run this again in a '
                  'terminal to answer, or add --old-library-stopped.')
            return False
        answer = input('Type "switch" to switch on this library, or press Return to stop here: ').strip().lower()
        if answer != 'switch':
            print('Stopped. The library is restored and checked, but not switched on. Run this again to switch it on.')
            return False
        return True
    return confirm


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--backup', type=Path)
    parser.add_argument('--media', choices=('in-place', 'copy'))
    parser.add_argument('--media-folder', type=Path)
    parser.add_argument('--allow-unprotected-media', action='store_true')
    parser.add_argument('--people', choices=('install', 'skip'))
    parser.add_argument('--people-port', type=int)
    parser.add_argument('--tls-name', action='append')
    parser.add_argument('--old-library-stopped', action='store_true')
    parser.add_argument('--switch-terminal', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--progress-lines', action='store_true',
                        help='Print progress as @@ JSON lines, for the setup window (app.migration.progress_lines)')
    from app.backup.passwords import add_arguments
    add_arguments(parser)
    args = parser.parse_args(argv)
    os.umask(0o077)
    from app.backup.diagnostics import ExplainedFailure
    from app.migration.move_in import Move, make_plan, unlock_backup
    from app.migration.settings_rules import UnknownSetting
    try:
        # Read first; asked again on every run, since none is kept.
        password = backup_password(args) if needs_backup(ROOT) else None
        if password is None and (args.backup_password_stdin or args.backup_password_frame_stdin):
            close_input()
        if args.progress_lines:
            from app.migration.progress_lines import ProgressLines
            progress = ProgressLines()
        else:
            progress = Progress()
        move = Move(installation(args.people_port or 54341), progress=progress)
        if move.begun():
            # Carrying on: the recorded choices stand, and any given again must be the same.
            given = {'backup': args.backup, 'media': args.media, 'media_directory': args.media_folder,
                     'people': args.people, 'names': args.tls_name, 'people_port': args.people_port}
            given = {name: (str(Path(os.path.abspath(value))) if isinstance(value, Path) else value)
                     for name, value in given.items() if value is not None}
            if args.allow_unprotected_media:
                given['allow_unprotected_media'] = True
            plan = move.open(given)
            # Only the stages that read the backup need its password; later ones run from the local copy.
            key = unlock_backup(plan['backup'], password) if move.needs_backup() else None
            if key is None:
                password = None
        else:
            if args.backup is None or args.media is None or args.media_folder is None or args.people is None:
                parser.error('A new restore needs --backup, --media, --media-folder and --people')
            move.open(make_plan(args.backup, password, args.media, args.media_folder, people=args.people,
                                allow_unprotected_media=args.allow_unprotected_media, names=args.tls_name,
                                people_port=args.people_port or 54341))
            key = unlock_backup(args.backup, password)
        result = move.run(key, confirm_switch(args.old_library_stopped, args.switch_terminal), password=password)
    except (ExplainedFailure, UnknownSetting, manage.BackupRunning) as reason:
        print(f'Restore stopped: {reason}', file=sys.stderr)
        return 1
    except Exception as error:
        # Database and tool errors can carry credentials or names; the logs folder has the detail.
        from app.backup.diagnostics import safe_categories
        print(f'Restore stopped ({type(error).__name__}: {", ".join(safe_categories(error)) or "see the logs folder"}). '
              'Nothing was deleted. Run the same command again to carry on from where it stopped.', file=sys.stderr)
        return 1
    if result['state'] != 'done':
        return 2
    print()
    print(f"FamilyFoto is running with the restored library: {result['media_files']:,} photos and videos.")
    print(f"  Web server: running. Open {result['url']} in your browser.")
    print('  Database server: running (private to FamilyFoto; not a web address).')
    print("Sign in with the old library's accounts. The administrator's password is the one that opened the backup.")
    print(f"The certificate covers {', '.join(result['certificate_names'])}. A device that trusted the old "
          f"computer trusts this one when it uses one of those names. Any other device needs "
          f"{result['certificate_authority']} trusted once.")
    from app.migration.move_in import people_summary
    print(people_summary(result.get('people')))
    print('Keep the backup until a new backup of this library has been made and verified.')
    print(json.dumps({'report': str(ROOT / 'restore/MOVE-IN-REPORT.json')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
