"""Run the native window's choices through the installer and keep its output for review."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from installer.macos import prepare
from installer.macos.restore_input import needs_backup, password_problem
from app.backup import passwords

RECORD = ROOT / '.macos-setup.json'


# Result codes consumed by SetupWindow. Child exit codes are normalized below.
ENTER_PASSWORD = 3
RECOVER_PEOPLE = 4


def save_record(arguments):
    with tempfile.NamedTemporaryFile(dir=ROOT, mode='w', delete=False) as file:
        json.dump(arguments, file)
    os.replace(file.name, RECORD)


def partial_people(arguments):
    receipt = ROOT / 'runtime/macos-people-install.json'
    if arguments[0] != 'install' or '--with-people' not in arguments or not receipt.exists():
        return False
    try:
        return json.loads(receipt.read_bytes()).get('state') == 'installing'
    except (ValueError, AttributeError):
        return True  # An interrupted receipt write also needs inspection.


def run(arguments, *, switch=False, secret=None):
    if RECORD.exists():
        recorded = json.loads(RECORD.read_bytes())
        if not switch and arguments != recorded:
            raise SystemExit('Another setup recorded different choices. Reopen the installer to carry on.')
        arguments = recorded
    elif switch:
        raise SystemExit('The setup record is missing. Reopen the installer.')
    else:
        prepare.require_fresh(ROOT)

    # Authenticate before creating the setup record or installing Core. Never persist the secret.
    if arguments[0] == 'restore' and needs_backup(ROOT):
        backup = Path(arguments[arguments.index('--backup') + 1])
        if secret is None:
            print('Enter the administrator password from when this backup was made.')
            return ENTER_PASSWORD
        if problem := password_problem(backup, secret):
            print(problem)
            return ENTER_PASSWORD
    if switch and arguments[0] == 'restore' and '--old-library-stopped' not in arguments:
        arguments.append('--old-library-stopped')
    save_record(arguments)

    if partial_people(arguments):
        return RECOVER_PEOPLE
    ready = (ROOT / 'runtime/macos-database-ready.json').is_file()
    commands = []
    if arguments[0] == 'restore' or not ready:
        commands.append([sys.executable, '-B', ROOT / 'installer/macos/workflow.py', *arguments])
    elif '--with-people' in arguments:
        receipt = ROOT / 'runtime/macos-people-install.json'
        if not receipt.exists():
            commands.append([ROOT / '.venv/bin/python3', ROOT / 'installer/macos/people.py', 'install',
                             '--research-only', '--port', arguments[arguments.index('--people-port') + 1]])
    if arguments[0] == 'install':
        commands.append([ROOT / '.venv/bin/python3', ROOT / 'installer/macos/manage.py', 'start', '--no-browser'])

    # logs/ may only be created by prepare after its fresh-copy check.
    with (ROOT / '.macos-setup.log').open('a', encoding='utf-8') as log:
        def emit(line):
            print(line, end='', flush=True)
            log.write(line)
            log.flush()

        for command in commands:
            if command[-1] == '--no-browser':
                emit('@@ {"stage":"started","state":"running"}\n')
            workflow = str(ROOT / 'installer/macos/workflow.py') in list(map(str, command))
            payload = None
            if workflow and secret is not None:
                payload = passwords.frame(secret) if arguments[0] == 'restore' else (secret + '\n').encode('utf-8')
            with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL) as child:
                if payload is not None:
                    try:
                        child.stdin.write(payload)
                        child.stdin.close()
                    except BrokenPipeError:
                        pass
                for line in child.stdout:
                    emit(line.decode('utf-8', errors='replace'))
                if code := child.wait():
                    if partial_people(arguments):
                        return RECOVER_PEOPLE
                    if code == 2:
                        journal = ROOT / 'restore/MOVE-IN.json'
                        waiting = journal.exists() and json.loads(journal.read_bytes()).get('waiting') == 'switch-warning'
                        return 2 if waiting else 1
                    return 1
    RECORD.unlink()
    return 0


if __name__ == '__main__':
    from installer.macos.setup_bootstrap import register_owner
    register_owner()
    os.umask(0o077)
    os.environ.update(PYTHONUNBUFFERED='1', PYTHONIOENCODING='utf-8', FAMILYFOTO_SETUP_WINDOW='1')
    with (ROOT / '.macos-setup.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit('Another setup window is running in this folder.')
        try:
            options = sys.argv[1:]
            recorded = json.loads(RECORD.read_bytes()) if RECORD.exists() else options
            secret = None
            if recorded[0] == 'restore' and needs_backup(ROOT):
                secret = passwords.read_frame(sys.stdin.buffer)
            elif recorded[0] == 'install' and not (ROOT / 'runtime/macos-database-ready.json').is_file():
                secret = passwords.read_line(sys.stdin.buffer)
            status = run(options, switch=options == ['--confirm-switch'], secret=secret)
        except passwords.PasswordProblem as error:
            print(str(error))
            status = ENTER_PASSWORD
        finally:
            if (ROOT / 'logs').is_dir() and (ROOT / '.macos-setup.log').is_file():
                with (ROOT / 'logs/macos-setup.log').open('ab') as dest, (ROOT / '.macos-setup.log').open('rb') as src:
                    shutil.copyfileobj(src, dest)
                (ROOT / '.macos-setup.log').unlink()
        sys.exit(status)
