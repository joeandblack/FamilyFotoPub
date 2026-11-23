"""Restore from a backup into this new Windows installation, in one go (app.migration.move_in).

Run by `install.ps1 -Mode Restore` with this installation's own Python, after Core is installed.
It asks what it needs that wasn't given (the backup, its administrator password, in place or a
copy, the library folder), checks them before anything is recorded, and runs every stage. Running
it again carries on from the stage that stopped, and asks for the password again: none is kept.

The password comes from the console, one line on stdin (--backup-password-stdin), or one framed
message on a private pipe from install.ps1 or the setup window (--backup-password-frame-stdin).
It is never an argument.
"""
import argparse
import csv
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

STAGE_TEXT = {
    'restored': 'Checking the backup and restoring the catalog and the photos and videos',
    'checked': 'Checking the restored library against the backup',
    'switched': 'Switching on the restored library',
    'thumbnails': 'Putting back the thumbnails, posters and face pictures',
    'installed': 'Moving the library into this installation',
    'restore-access': 'Checking desktop access to restored media, thumbnails and People jobs',
    'people': 'Setting up People on this computer (this can download the face model or its libraries)',
    'started': 'Starting FamilyFoto',
    'repaired': 'Rebuilding any thumbnail or poster still missing',
}
# On Windows the database cluster and the setup record live in `data`, and stay (move_in.Installation.keep_data).
KEEP_DATA = ('postgres', 'setup')
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class Progress:
    """Plain lines for a person at the console: each stage, and a count now and then. A setup window
    gets app.migration.progress_lines.ProgressLines instead (--progress-lines)."""

    def __init__(self):
        self.shown = 0

    def __call__(self, event):
        if not isinstance(event, dict):
            return
        if 'stage' in event:
            text = STAGE_TEXT.get(event['stage'], event['stage'])
            print(('... ' if event['state'] == 'running' else 'OK  ') + text, flush=True)
            return
        moment = time.monotonic()
        if moment - self.shown < 15:
            return
        self.shown = moment
        parts = [str(event.get('phase', 'working'))]
        for name in ('files', 'total_files', 'bytes', 'total_bytes'):
            if isinstance(event.get(name), int):
                parts.append(f'{name.replace("_", " ")} {event[name]:,}')
        print('    ' + ', '.join(parts), flush=True)


def settings():
    # Not load_config: it refuses this installation while the restore holds it.
    return json.loads((ROOT / 'config/local.json').read_text(encoding='utf-8'))


def powershell(command, timeout=120):
    return subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True,
                          text=True, timeout=timeout, creationflags=NO_WINDOW)


def listening(port):
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(('127.0.0.1', port)) == 0


def wait_for(port, seconds, what):
    deadline = time.monotonic() + seconds
    while not listening(port):
        if time.monotonic() > deadline:
            raise SystemExit(f'{what} did not start in time. See the logs folder.')
        time.sleep(1)


@contextmanager
def database(config, names):
    """This installation's own database service, which stays running: FamilyFoto needs it after."""
    if listening(config['port']):
        raise SystemExit(f"Something is already using web port {config['port']}. Stop FamilyFoto in this installation first.")
    service = names['database_service']
    result = powershell(f"Start-Service -Name '{service}'")
    if result.returncode:
        raise SystemExit(f'The database service {service} could not be started. Run this from an administrator PowerShell.')
    wait_for(config['database']['port'], 60, f'The database service {service}')
    try:
        yield
    finally:
        from app.database import close_pools
        close_pools()


def start(config, names):
    """Start this installation's own server task, and wait until it answers."""
    task = names['server_task']
    result = powershell(f"Start-ScheduledTask -TaskName '{task}'")
    if result.returncode:
        raise SystemExit(f"The task '{task}' could not be started. Start FamilyFoto with its desktop shortcut.")
    wait_for(config['port'], 120, 'FamilyFoto')


def restore_desktop_access(root):
    """Grant only the recorded installer/desktop account access to the restored runtime trees."""
    from app.backup.diagnostics import ExplainedFailure
    from app.backup.space import directory_identity

    data = root / 'data'
    directory_identity(data / 'setup')
    record = json.loads((data / 'setup/install.json').read_text(encoding='utf-8-sig'))
    system = Path(os.environ['SystemRoot']) / 'System32'
    identity = subprocess.check_output([system / 'whoami.exe', '/user', '/fo', 'csv', '/nh'],
                                       creationflags=NO_WINDOW).decode(errors='replace')
    sid = next(csv.reader([identity.strip()]))[1]
    if record['owner'] != sid or Path(record['root']) != root:
        raise ExplainedFailure('Run the restore from the original installation and Windows account.')

    from installer.windows.restore_acl import repair_trees, repair_media
    journal = json.loads((root / 'restore/MOVE-IN.json').read_text(encoding='utf-8'))
    config = json.loads((root / 'config/local.json').read_text(encoding='utf-8'))
    home = Path(journal['plan']['media_directory'])
    media = home / 'media-copy/media'
    backup = Path(journal['plan']['backup'])
    if (not home.is_absolute() or home == backup or home.is_relative_to(backup)
            or Path(journal['results']['media_root']) != media
            or Path(config['destination']['root']) != media):
        raise ExplainedFailure('Restored media location does not match the recorded restore; no access repair performed.')
    from app.migration.disk import disk_binding
    from app.storage.disks import match_disk
    match_disk(config['destination'], disk_binding(media))
    repair_trees(data, sid)
    repair_media(home, sid, allow_unprotected=journal['plan'].get('allow_unprotected_media') is True)


def certificate_names():
    """This computer's own names. Windows finds computers on a home network by their plain name,
    not the Mac's NAME.local; LAN names can be added with --tls-name."""
    names = ['127.0.0.1', 'localhost']
    host = socket.gethostname().lower()
    if host and host not in names:
        names.append(host)
    return names


def installation(people_port):
    from app.backup.postgres import Endpoint
    from app.migration.move_in import Installation
    from app.service.instance import names as instance_names
    config = settings()
    names = instance_names(config)
    binary = ROOT / 'runtime/postgres/bin'
    admin = Endpoint('127.0.0.1', config['database']['port'], 'postgres', 'postgres',
                     password=config['postgres_admin_password'])
    return Installation(root=ROOT, admin=admin, cluster=(ROOT / 'data/postgres').resolve(),
                        pg_restore=binary / 'pg_restore.exe', pg_dump=binary / 'pg_dump.exe', web_port=config['port'],
                        people_port=people_port, database=lambda: database(config, names),
                        start=lambda: start(config, names), python=ROOT / '.venv/Scripts/python.exe',
                        keep_data=KEEP_DATA, restore_access=lambda: restore_desktop_access(ROOT),
                        restore_access_stage='restore-access-media-v2')


# Questions, for a person at the console. Each has a plain typed fallback.

def pick(kind, title):
    try:
        import tkinter
        from tkinter import filedialog
        window = tkinter.Tk()
        window.withdraw()
        window.attributes('-topmost', True)
        chosen = (filedialog.askopenfilename(title=title) if kind == 'file'
                  else filedialog.askdirectory(title=title, mustexist=True))
        window.destroy()
        if chosen:
            return Path(chosen)
    except Exception:
        pass
    typed = input(f'{title} (type the full path): ').strip().strip('"')
    return Path(typed) if typed else None


def ask_password():
    import getpass
    from app.backup.passwords import checked_text
    return checked_text(getpass.getpass('FamilyFoto administrator password from the old computer '
                                        '(as it was when this backup was made): '))


def working_password(backup, password, can_ask, ask_for=ask_password):
    """The password and the backup's key, once it opens this backup. A person at the console is
    asked again after a wrong one; a password that came on stdin or a pipe is final."""
    from app.backup.diagnostics import ExplainedFailure
    from app.migration.move_in import WrongBackupPassword, unlock_backup
    while True:
        if password is None:
            if not can_ask:
                raise ExplainedFailure('An unattended restore needs the administrator password on stdin '
                                       '(--backup-password-stdin, or -BackupPasswordFromStdin in install.ps1).')
            password = ask_for()
        try:
            return password, unlock_backup(backup, password)
        except WrongBackupPassword as problem:
            if not can_ask:
                raise
            print(f'\n{problem}\nType it again.')
            password = None


def given_password(args):
    """A password that came with the command (pipe or stdin), read once, or None to ask."""
    from app.backup import passwords
    if args.backup_password_frame_stdin or args.backup_password_stdin:
        return passwords.from_arguments(args)
    return None


def ask(args, ask_input=input, choose=pick):
    """Fill in what wasn't given. The backup and the folder are each checked before anything
    is recorded, and one that won't do is asked for again."""
    from app.backup.diagnostics import ExplainedFailure
    from app.migration.media_folder import UnsuitableMediaFolder, check_media_folder
    while args.backup is None or not (Path(args.backup) / 'catalog').is_dir():
        if args.backup is not None:
            if args.unattended:
                raise ExplainedFailure(f'{args.backup} is not a FamilyFoto backup folder: it has no catalog.')
            print(f'\n{args.backup} is not a FamilyFoto backup folder: it has no "catalog" folder. Choose it again.')
        args.backup = choose('folder', 'Choose the FamilyFoto backup folder (the one holding "catalog")')
    while True:
        if args.media is None:
            print()
            print('How should the photos and videos come back?')
            print('  1. In place: use the copy on the backup disk itself. Fast, and needs no space elsewhere, but the')
            print('     backup disk becomes the library disk, and this backup is used up.')
            print('  2. Copy: copy them to a folder you choose. Slower, and needs room for them, but the backup stays as it is.')
            answer = ask_input('Type 1 or 2: ').strip()
            args.media = {'1': 'in-place', '2': 'copy'}.get(answer)
            if args.media is None:
                continue
        if args.media_folder is None:
            parent = choose('folder', 'Choose where to make the new library folder' +
                            (' (on the backup disk)' if args.media == 'in-place' else ''))
            if parent is None:
                continue
            args.media_folder = parent / 'FFLibrary'
            print(f'The library folder will be {args.media_folder}')
        try:
            check_media_folder(args.backup, args.media, args.media_folder, allow_unprotected=args.allow_unprotected_media)
            return args
        except UnsuitableMediaFolder as problem:
            if args.unattended:
                # No one to ask: say which flag accepts it, rather than reading a question no one sees.
                if problem.unprotected:
                    raise UnsuitableMediaFolder(f'{problem} Unattended, add -AllowUnprotectedMedia to accept it.',
                                                unprotected=True) from None
                raise
            print(f'\n{problem}')
            if problem.unprotected and ask_input('Accept that, and use this folder? Type yes or no: ').strip().lower() in ('yes', 'y'):
                args.allow_unprotected_media = True
                continue
            args.media = args.media_folder = None


def confirm_switch(automatic, interactive):
    from app.migration.move_in import WARNING

    def confirm(summary):
        print()
        print(WARNING)
        if summary.get('media_files') is not None:
            print(f"The restored library holds {summary['media_files']:,} photos and videos, checked against the backup.")
        if automatic:
            print('Confirmed with -OldLibraryStopped.')
            return True
        if not interactive:
            print('Stopping here: the library is restored and checked, but not switched on. Run this again in a '
                  'console to answer, or add -OldLibraryStopped.')
            return False
        answer = input('Type "switch" to switch on this library, or press Enter to stop here: ').strip().lower()
        if answer != 'switch':
            print('Stopped. The library is restored and checked, but not switched on. Run this again to switch it on.')
            return False
        return True
    return confirm


def parser():
    result = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    result.add_argument('--backup', type=Path)
    result.add_argument('--media', choices=('in-place', 'copy'))
    result.add_argument('--media-folder', type=Path)
    result.add_argument('--allow-unprotected-media', action='store_true')
    result.add_argument('--people', choices=('install', 'skip'))
    result.add_argument('--people-port', type=int, required=True)
    result.add_argument('--tls-name', action='append')
    result.add_argument('--old-library-stopped', action='store_true')
    result.add_argument('--unattended', action='store_true')
    result.add_argument('--progress-lines', action='store_true',
                        help='Report progress as @@ JSON lines, for the setup dialog')
    from app.backup.passwords import add_arguments
    add_arguments(result)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    from app.backup.diagnostics import ExplainedFailure
    from app.migration.media_folder import UnsuitableMediaFolder
    from app.migration.move_in import Move, STAGES, make_plan
    from app.migration.progress_lines import ProgressLines
    from app.migration.settings_rules import UnknownSetting
    try:
        # Read now, before anything else can take stdin; asked at the console later if not given.
        password = given_password(args)
        can_ask = password is None and not args.unattended
        move = Move(installation(args.people_port), progress=ProgressLines() if args.progress_lines else Progress())
        if move.begun():
            # Carrying on: the recorded choices stand, and any given again must be the same.
            given = {'backup': args.backup, 'media': args.media, 'media_directory': args.media_folder,
                     'people': args.people, 'names': args.tls_name, 'people_port': args.people_port}
            given = {name: (str(Path(os.path.abspath(value))) if isinstance(value, Path) else value)
                     for name, value in given.items() if value is not None}
            if args.allow_unprotected_media:
                given['allow_unprotected_media'] = True
            plan = move.open(given)
            if all(move.done(stage) for stage in STAGES):
                # All backup-dependent stages finished. run() also republishes
                # the final report if the previous run stopped after its last stage.
                move.run(None, confirm_switch(False, False))
                print('Completed restore: desktop access is ready. If you stopped FamilyFoto, restart it from its desktop shortcut.')
                return 0
            # No password is kept between runs: it opens the recorded backup again, but only for the
            # stages that read it. Later ones run from the local copy, with the backup disk away.
            if move.needs_backup():
                password, key = working_password(plan['backup'], password, can_ask)
            else:
                password = key = None
        else:
            if args.people is None:
                raise ExplainedFailure('Choose whether to install People: run it from install.ps1, which asks.')
            if args.unattended and None in (args.backup, args.media, args.media_folder):
                raise ExplainedFailure('An unattended restore needs -BackupDirectory, -Media and -MediaFolder.')
            args = ask(args)
            password, key = working_password(args.backup, password, can_ask)
            plan = move.open(make_plan(args.backup, password, args.media, args.media_folder, people=args.people,
                                       allow_unprotected_media=args.allow_unprotected_media,
                                       names=args.tls_name or certificate_names(), people_port=args.people_port))
        result = move.run(key, confirm_switch(args.old_library_stopped, not args.unattended), password=password)
    except (ExplainedFailure, UnknownSetting, UnsuitableMediaFolder) as reason:
        print(f'Restore stopped: {reason}', file=sys.stderr)
        return 1
    except Exception as error:
        # Database and tool errors can carry credentials or names; the logs folder has the detail.
        from app.backup.diagnostics import safe_categories
        print(f'Restore stopped ({type(error).__name__}: {", ".join(safe_categories(error)) or "see the logs folder"}). '
              'Nothing was deleted. Run -Mode Restore again to carry on from where it stopped.', file=sys.stderr)
        return 1
    if result['state'] != 'done':
        return 2
    print()
    print(f"FamilyFoto is running at {result['url']} with the restored library: {result['media_files']:,} photos and videos.")
    print("Sign in with the old library's accounts. The administrator's password is the one that opened the backup.")
    print(f"The certificate covers {', '.join(result['certificate_names'])}. A device that trusted the old computer "
          f"trusts this one when it uses one of those names. Any other device needs {result['certificate_authority']} "
          'trusted once.')
    people = result.get('people')
    if isinstance(people, dict):
        check = people.get('library_check') or {}
        print('People: ' + ('set up, and editable.' if check.get('passed') else
                            f"installed, but the library check did not pass ({check.get('reason')}). People can be viewed, not edited."))
    else:
        print('People: not installed. The saved people can be viewed; install People later to edit them.')
    print('Keep the backup until a new backup of this library has been made and verified.')
    print(json.dumps({'report': str(ROOT / 'restore/MOVE-IN-REPORT.json')}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
