"""Coordinate a fresh Core installation, People, and a restore from a backup."""
import argparse
import os
from pathlib import Path
import platform
import secrets
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from installer import configure
from installer.macos import bundled, prepare
from installer.macos.restore_input import needs_backup, password_problem, consume_password, close_input
from app.backup import passwords

# The same words as the Windows installer's question ($PeopleTermsText in setup-common.ps1).
PEOPLE_TERMS = '''People finds faces in your photos and groups them, so you can name the people in them.
It uses the InsightFace face model (buffalo_l), which its authors license for
non-commercial research use only. The model is downloaded during installation.
Terms: https://github.com/deepinsight/insightface/tree/master/python-package'''


def people_terms():
    """PEOPLE_TERMS, except that a shared-installer release already carries the model."""
    if bundled.active():
        return PEOPLE_TERMS.replace('The model is downloaded during installation.',
                                    'The model is included with this installer; its libraries are '
                                    'downloaded from the Python Package Index during installation.')
    return PEOPLE_TERMS


def interactive():
    return sys.stdin.isatty() and sys.stdout.isatty()


def people_choice(args, ask=input):
    """'install' or 'skip'. A flag decides, or the person answers; there is no silent default."""
    if args.with_people and args.without_people:
        raise SystemExit('Choose either --with-people or --without-people, not both. No installation started.')
    if args.research_only and not args.with_people:
        raise SystemExit('--research-only accepts the face model terms, so it goes with --with-people. No installation started.')
    if args.with_people and not args.research_only:
        raise SystemExit('People needs explicit --research-only consent to the model terms. No installation started.')
    if args.with_people:
        return 'install'
    if args.without_people:
        return 'skip'
    if not interactive():
        raise SystemExit('Choose whether to install People: add --with-people --research-only (accepting the face '
                         'model terms) or --without-people. No installation started.')
    print('\nInstall People (faces)?\n' + people_terms())
    for _ in range(3):
        answer = ask('Install People and accept these terms? Type yes or no: ').strip().lower()
        if answer in ('yes', 'y'):
            return 'install'
        if answer in ('no', 'n'):
            return 'skip'
    raise SystemExit('No answer about People. Run the installer again and answer yes or no. No installation started.')


def choose_folder(prompt):
    script = f'POSIX path of (choose folder with prompt "{prompt}")'
    result = subprocess.run(['/usr/bin/osascript', '-e', script], capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        return Path(result.stdout.strip())
    return Path(input(f'{prompt} (type its full path): ').strip()).expanduser()


def ask_restore(args):
    """Fill in what a restore needs that wasn't given, by asking. Unattended, everything must be given."""
    missing = [name for name in ('backup', 'media', 'media_folder') if getattr(args, name) is None]
    if missing and not interactive():
        raise SystemExit('A restore needs --backup, --media in-place|copy and --media-folder. '
                         'No installation started.')
    if args.backup is None:
        print('\nChoose the backup folder: the folder the backup page made, holding catalog and media-copy.')
        args.backup = choose_folder('Choose the FamilyFoto backup folder')
    if not interactive():
        return args  # Everything was given; check_restore_choices says what is wrong, with the flag to fix it.
    while True:
        if args.media is None:
            print('\nHow should the photos and videos be restored?\n'
                  '  1. In place: use them where they are. They are moved within the backup disk into a new\n'
                  '     library folder there, in minutes whatever the size, with no extra space needed. That\n'
                  '     disk becomes the library\'s photo disk, and the backup folder is no longer a backup.\n'
                  '     The way for a large library. The library folder must be on the backup disk.\n'
                  '  2. Copy them to another folder, on this Mac or another disk. The backup stays a complete\n'
                  '     backup. Needs as much free space as the library, and takes as long as copying it\n'
                  '     (hours for terabytes).')
            while args.media is None:
                answer = input('Type 1 or 2: ').strip()
                args.media = {'1': 'in-place', '2': 'copy'}.get(answer)
        if args.media_folder is None:
            where = ('on the backup disk' if args.media == 'in-place' else 'where the copy should go')
            print(f'\nChoose the folder to hold the new library folder, {where}.')
            parent = choose_folder('Choose where the library folder goes')
            args.media_folder = parent / 'FFLibrary'
            print(f'The library folder will be {args.media_folder}')
        problem = media_folder_problem(args)
        if problem is None:
            return args
        print('\n' + str(problem))
        if problem.unprotected and ask_yes('Accept that anyone with the disk can read the photos and videos?'):
            args.allow_unprotected_media = True
            continue
        # Ask both again: the answer to a wrong disk may be to copy instead.
        args.media = args.media_folder = None


def ask_yes(question, ask=None):
    ask = ask or input
    for _ in range(3):
        answer = ask(question + ' Type yes or no: ').strip().lower()
        if answer in ('yes', 'y'):
            return True
        if answer in ('no', 'n'):
            return False
    return False


def media_folder_problem(args):
    """What the restore would refuse about the library folder, found before Core is installed."""
    from app.migration.media_folder import UnsuitableMediaFolder, check_media_folder
    try:
        check_media_folder(args.backup, args.media, args.media_folder, allow_unprotected=args.allow_unprotected_media)
    except UnsuitableMediaFolder as problem:
        return problem
    return None


def check_restore_choices(args):
    """Unattended or interactive, a wrong folder is found before anything is installed or recorded."""
    if not (Path(args.backup) / 'catalog').is_dir():
        raise SystemExit(f'{args.backup} is not a FamilyFoto backup folder: it has no catalog. No installation started.')
    problem = media_folder_problem(args)
    if problem is not None:
        hint = ' Add --allow-unprotected-media to accept that.' if problem.unprotected else ''
        raise SystemExit(f'{problem}{hint} No installation started.')


def core_installed():
    """Core finished installing: its database is ready. A restore that stopped before it began carries on from here."""
    return (ROOT / 'runtime/macos-database-ready.json').is_file()


def one_line(args, people):
    """The same restore as one command, short flags, for running it again or unattended.
    Only paths and choices: the password is prompted for, never printed."""
    parts = ['bash', 'installer/macos/install.sh', 'restore', '-b', str(args.backup),
             '-m', args.media, '-f', str(args.media_folder)]
    if args.allow_unprotected_media:
        parts.append('--allow-unprotected-media')
    parts += ['--with-people', '--research-only'] if people == 'install' else ['--without-people']
    if args.people_port is not None and people == 'install':
        parts += ['--people-port', str(args.people_port)]
    for name in args.tls_name or ():
        parts += ['--tls-name', name]
    return ' '.join(shlex.quote(part) for part in parts)


def show_one_line(args, people):
    print('\nThe same restore as one command, from this folder (it asks for the administrator password; add --old-library-stopped '
          'to answer the switch warning too):\n  ' + one_line(args, people) + '\n')


def restore_command(args, people):
    command = [str(ROOT / '.venv/bin/python3'), str(ROOT / 'installer/macos/restore.py')]
    for flag, value in (('--backup', args.backup), ('--media', args.media), ('--media-folder', args.media_folder),
                        ('--people', people), ('--people-port', args.people_port)):
        if value is not None:
            command += [flag, str(value)]
    for name in args.tls_name or ():
        command += ['--tls-name', name]
    if args.allow_unprotected_media:
        command.append('--allow-unprotected-media')
    if args.old_library_stopped:
        command.append('--old-library-stopped')
    if getattr(args, 'progress_lines', False):
        command.append('--progress-lines')
    return command


def run_restore(args, people, password=None):
    """The restore runs with the installation's own Python, and in the foreground, to ask the warning."""
    environment = {**os.environ, 'LC_ALL': 'C', 'LANG': 'C'}
    for name in ('FAMILYFOTO_CONFIG_DIR', 'FAMILYFOTO_DATA_DIR', 'FAMILYFOTO_LOG_DIR', 'FAMILYFOTO_TEMP_DIR'):
        environment.pop(name, None)
    command = restore_command(args, people)
    if interactive():
        command.append('--switch-terminal')
    if password is not None:
        command.append('--backup-password-frame-stdin')
        return subprocess.run(command, cwd=ROOT, env=environment, input=passwords.frame(password)).returncode
    return subprocess.run(command, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL).returncode


def tool_dirs():
    """PostgreSQL and FFmpeg `bin` folders: bundled by the shared installer, else Homebrew."""
    if bundled.active():
        return bundled.program_dir('postgres'), bundled.program_dir('ffmpeg')
    brew = '/opt/homebrew/bin/brew'
    env = {**os.environ, 'HOMEBREW_NO_AUTO_UPDATE': '1',
           'HOMEBREW_NO_INSTALL_CLEANUP': '1', 'HOMEBREW_NO_ANALYTICS': '1'}
    for formula in ('python@3.12', 'postgresql@17', 'ffmpeg'):
        if subprocess.run([brew, 'list', '--versions', formula], stdout=subprocess.DEVNULL, env=env).returncode:
            subprocess.run([brew, 'install', formula], check=True, env=env)
    def prefix(formula):
        return Path(subprocess.check_output([brew, '--prefix', formula], env=env, text=True).strip())
    return prefix('postgresql@17') / 'bin', prefix('ffmpeg') / 'bin'


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('prepare', 'install', 'restore'))
    p.add_argument('--no-dialog', action='store_true', help='Use the terminal questions')
    p.add_argument('--instance', type=configure.instance_name)
    p.add_argument('--with-people', action='store_true')
    p.add_argument('--without-people', action='store_true')
    p.add_argument('--research-only', action='store_true')
    p.add_argument('--password-stdin', action='store_true')
    p.add_argument('--database-port', type=int, default=5433)
    p.add_argument('--web-port', type=int, default=4341)
    p.add_argument('--people-port', type=int, help='Default 54341')
    restore = p.add_argument_group('restore from a backup')
    restore.add_argument('-b', '--backup', type=Path, help='The backup folder')
    passwords.add_arguments(restore)
    restore.add_argument('-m', '--media', choices=('in-place', 'copy'))
    restore.add_argument('-f', '--media-folder', type=Path, help='The new library folder for the photos and videos')
    restore.add_argument('--allow-unprotected-media', action='store_true',
                         help='Accept a library folder on a disk without permissions, such as exFAT')
    restore.add_argument('--tls-name', action='append', help='A name or address this Mac answers to (repeatable)')
    restore.add_argument('--old-library-stopped', action='store_true',
                         help="Confirm the switch warning: the old computer's FamilyFoto is stopped for good")
    restore.add_argument('--progress-lines', action='store_true',
                         help='Print the restore\'s progress as @@ JSON lines, for the setup window')
    return p


def main():
    from installer.macos.setup_bootstrap import register_owner
    register_owner()
    args = parser().parse_args()
    if args.password_stdin and (args.backup_password_stdin or args.backup_password_frame_stdin):
        raise SystemExit('Account and backup password input cannot be combined.')
    # Consume redirected secrets before any subprocess (including folder pickers or brew).
    secret = None
    if args.backup_password_stdin or args.backup_password_frame_stdin:
        if needs_backup(ROOT):
            secret = consume_password(args)
        else:
            close_input()
    account_password = None
    if args.password_stdin:
        account_password = passwords.read_line(sys.stdin.buffer)
        close_input()
    if sys.platform != 'darwin' or platform.machine() != 'arm64' or os.geteuid() == 0:
        print('Native Apple Silicon signed-in user required.', file=sys.stderr)
        return 1
    if args.mode == 'prepare' and (args.with_people or args.without_people or args.backup):
        print('prepare sets up the environment only; People and restoring go with install or restore.', file=sys.stderr)
        return 2
    if args.mode == 'install' and args.backup is None and interactive():
        print('Install FamilyFoto with:\n  1. A new library\n  2. A library restored from a backup '
              '(moving it from another computer)')
        choice = None
        while choice not in ('1', '2'):
            choice = input('Type 1 or 2: ').strip()
        if choice == '2':
            args.mode = 'restore'
    if args.mode == 'install' and args.backup is not None:
        args.mode = 'restore'
    if args.mode == 'restore' and (ROOT / 'restore/MOVE-IN.json').is_file():
        # Carrying on with a restore: Core is installed, and the recorded choices stand.
        if needs_backup(ROOT) and secret is None:
            secret = passwords.prompt()
        people = 'install' if args.with_people else 'skip' if args.without_people else None
        return run_restore(args, people, secret)
    if args.mode == 'restore' and core_installed():
        # Core is installed but the restore never began (it stopped before recording any choice):
        # ask again, and restore into this installation without installing Core again.
        people = people_choice(args)
        args = ask_restore(args)
        check_restore_choices(args)
        if secret is None:
            secret = passwords.prompt()
        if problem := password_problem(args.backup, secret):
            raise SystemExit(problem + ' No installation started.')
        show_one_line(args, people)
        print('Core is already installed. Restoring the library from the backup.')
        return run_restore(args, people, secret)
    people = people_choice(args) if args.mode in ('install', 'restore') else None
    if args.mode == 'restore':
        args = ask_restore(args)
        check_restore_choices(args)
        if secret is None:
            secret = passwords.prompt()
        if problem := password_problem(args.backup, secret):
            raise SystemExit(problem + ' No installation started.')
        show_one_line(args, people)
    if args.people_port is None:
        args.people_port = 54341
    try:
        ports = [args.database_port, args.web_port] + ([args.people_port] if people == 'install' else [])
        if len(set(ports)) != len(ports) or any(not 1024 <= n <= 65535 for n in ports):
            raise ValueError('Invalid or overlapping ports')
        if people == 'install' and not 49152 <= args.people_port <= 65535:
            raise ValueError('People port must be 49152-65535')
        os.umask(0o077)
        # Directory flock needs no persistent lock file and protects concurrent Core setup.
        import fcntl  # Mac only; imported here so the rest of this module can be tested anywhere.
        descriptor = os.open(ROOT, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            prepare.require_fresh(ROOT)
            prepare.require_free_ports(ports)
            postgres_bin, ffmpeg_bin = tool_dirs()
            # A restored library brings its own accounts, so nobody needs this password.
            password = secrets.token_urlsafe(32) if args.mode == 'restore' else account_password
            prepare.prepare(ROOT, postgres_bin, ffmpeg_bin,
                            args.database_port, args.web_port, args.password_stdin, password=password, instance=args.instance)
            if args.mode in ('install', 'restore'):
                python = str(ROOT / '.venv/bin/python3')
                subprocess.run([python, str(ROOT / 'installer/macos/manage.py'), 'install'], check=True,
                               env={**os.environ, 'LC_ALL': 'C', 'LANG': 'C'})
                if args.mode == 'install' and people == 'install':
                    subprocess.run([python, str(ROOT / 'installer/macos/people.py'), 'install',
                                    '--research-only', '--port', str(args.people_port)], check=True)
                if args.mode == 'install':
                    print('Installation complete. FamilyFoto is not running yet: its database server, web server'
                          + (' and People service' if people == 'install' else '') + ' are stopped.\n'
                          'Start it with the FamilyFoto app in this folder; it prints the web address to open.')
                    if people == 'skip':
                        print('People was not installed, as chosen. Install it later with installer/macos/people.py.')
        finally:
            os.close(descriptor)
    except Exception as error:
        print('Installation stopped (' + type(error).__name__ + '). Preserve partial state. '
              'If Core completed, do not rerun Core; inspect People setup separately.', file=sys.stderr)
        return 1
    if args.mode == 'restore':
        print('Core is installed. Restoring the library from the backup.')
        return run_restore(args, people, secret)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except passwords.PasswordProblem as error:
        raise SystemExit(str(error)) from None
