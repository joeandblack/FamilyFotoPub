"""On-demand Mac lifecycle. Never adopts Homebrew's default database cluster."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
RUNTIME = ROOT / 'runtime'
CLUSTER = RUNTIME / 'postgres'
STOP = RUNTIME / 'macos-stop'
LABEL = 'local.familyfoto.' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
DOMAIN = f'gui/{os.getuid()}' if hasattr(os, 'getuid') else ''
PLIST = RUNTIME / 'macos-service.plist'
PYTHON = ROOT / '.venv/bin/python3'


class BackupRunning(RuntimeError):
    """A message we wrote ourselves, with no paths or credentials, so it is safe to show."""


def settings(restoring=False):
    """`restoring` is only for the restore into this installation, which runs its database
    while the installation is still refused to everything else."""
    c = json.loads((ROOT / 'config/local.json').read_text())
    if not restoring and c.get('recovery') and c['recovery'].get('activation_required') is not False:
        # A restore into this installation that hasn't been switched on and put in place yet.
        raise BackupRunning('A restore into this installation is not finished. Run '
                            'bash installer/macos/install.sh restore again to finish it.')
    if c['database']['host'] != '127.0.0.1' or c.get('host') != '127.0.0.1':
        raise ValueError('Mac service requires loopback-only configuration')
    ports = (c['database']['port'], c['port'])
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in ports) or ports[0] == ports[1]:
        raise ValueError('Invalid or overlapping ports')
    receipt = json.loads((RUNTIME / 'macos-preparation.json').read_text())
    if receipt['state'] != 'prepared-not-installed' or receipt['root'] != str(ROOT):
        raise ValueError('Completed preparation for this directory is required')
    return c, Path(receipt['tools']['postgres']).parent


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, cwd=ROOT, **kwargs)


def free_ports(c):
    for port in (c['database']['port'], c['port']):
        with socket.socket() as probe:
            # Match server rebinding after graceful shutdown; active listeners still conflict.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', port))


def database_identity(c):
    import psycopg
    with psycopg.connect(host='127.0.0.1', port=c['database']['port'], dbname='postgres',
                          user='postgres', password=c['postgres_admin_password'], connect_timeout=2) as db:
        actual = db.execute('SHOW data_directory').fetchone()[0]
        if Path(actual).resolve() != CLUSTER.resolve():
            raise ValueError('Unexpected database directory; refusing to adopt it')


def wait_database(c, process):
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError('Private PostgreSQL exited; see logs/macos-postgres.log')
        try:
            database_identity(c)
            return
        except Exception:
            time.sleep(.25)
    raise RuntimeError('Private PostgreSQL did not become ready')


def postgres_args(c, binary):
    return [str(binary / 'postgres'), '-D', str(CLUSTER), '-h', '127.0.0.1',
            '-p', str(c['database']['port']), '-k', '', '-c', 'password_encryption=scram-sha-256']


def initialize():
    c, binary = settings()
    if CLUSTER.exists():
        raise ValueError('Private cluster already exists; use start, or inspect an interrupted installation')
    free_ports(c)
    with tempfile.NamedTemporaryFile(mode='w', dir=RUNTIME, prefix='.initdb-password-') as password:
        os.chmod(password.name, 0o600)
        password.write(c['postgres_admin_password'] + '\n')
        password.flush()
        run([binary / 'initdb', '-D', CLUSTER, '-U', 'postgres', '--auth-local=scram-sha-256',
             '--auth-host=scram-sha-256', '--encoding=UTF8', '--locale=C', '--pwfile=' + password.name,
             # Its own advice ("pg_ctl ... -l logfile start") would start the database server alone, by
             # hand, which isn't how FamilyFoto starts. FamilyFoto starts and stops it itself.
             '--no-instructions'])
    with (ROOT / 'logs/macos-postgres.log').open('a') as log:
        db = subprocess.Popen(postgres_args(c, binary), stdout=log, stderr=log)
        try:
            wait_database(c, db)
            run([PYTHON, ROOT / 'installer/init-database.py'])
            run([PYTHON, ROOT / 'installer/verify-installation.py', '--database-only', '--before-schema'])
            run([PYTHON, '-m', 'app.database'])
            run([PYTHON, ROOT / 'installer/verify-installation.py', '--require-empty'])
            (RUNTIME / 'macos-database-ready.json').write_text(json.dumps({'cluster':str(CLUSTER), 'port':c['database']['port']}))
            print(f"Database server: set up (PostgreSQL, private to FamilyFoto, port {c['database']['port']}), "
                  'and stopped again. FamilyFoto starts and stops it itself: never start it by hand.')
        finally:
            if db.poll() is None:
                db.send_signal(signal.SIGTERM)  # PostgreSQL smart shutdown, never forced.
                db.wait(timeout=60)


def validate_ready(c):
    ready = json.loads((RUNTIME / 'macos-database-ready.json').read_text())
    if ready != {'cluster':str(CLUSTER), 'port':c['database']['port']} or (CLUSTER / 'PG_VERSION').read_text().strip() != '17':
        raise ValueError('Private database installation identity mismatch')


def service():
    import fcntl
    c, binary = settings()
    validate_ready(c)
    with (RUNTIME / 'macos-service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        free_ports(c)
        STOP.unlink(missing_ok=True)
        stopping = False
        def stop_signal(*_):
            nonlocal stopping
            stopping = True
        signal.signal(signal.SIGTERM, stop_signal)
        signal.signal(signal.SIGINT, stop_signal)
        with (ROOT / 'logs/macos-postgres.log').open('a') as log:
            db = subprocess.Popen(postgres_args(c, binary), stdout=log, stderr=log)
            web = None
            try:
                wait_database(c, db)
                run([PYTHON, ROOT / 'installer/verify-installation.py', '--database-only'])
                from app.service.macos_lifecycle import web_paused
                while not stopping and not STOP.exists():
                    if db.poll() is not None:
                        raise RuntimeError('A FamilyFoto service exited; inspect logs')
                    if web_paused():
                        # An administrator's backup needs the database but no web server.
                        if web is not None:
                            web.send_signal(signal.SIGTERM)
                            web.wait()
                            web = None
                    elif web is None:
                        web = subprocess.Popen([str(PYTHON), '-m', 'app.service.macos'], cwd=ROOT)
                    elif web.poll() is not None:
                        raise RuntimeError('A FamilyFoto service exited; inspect logs')
                    time.sleep(.5)
            finally:
                from app.service.macos_lifecycle import request_pause
                request_pause()
                if web is not None and web.poll() is None:
                    web.send_signal(signal.SIGTERM)
                    web.wait()  # No forced termination or shutdown deadline.
                if db.poll() is None:
                    db.send_signal(signal.SIGTERM)
                    db.wait()
                STOP.unlink(missing_ok=True)


def plist():
    return {'Label':LABEL, 'ProgramArguments':[str(PYTHON), str(Path(__file__).resolve()), 'serve'],
            'WorkingDirectory':str(ROOT), 'RunAtLoad':True, 'KeepAlive':False,
            'ExitTimeOut':0, 'Umask':0o077, 'EnvironmentVariables':{'LC_ALL':'C', 'LANG':'C'},
            'StandardOutPath':str(ROOT / 'logs/macos-service.log'),
            'StandardErrorPath':str(ROOT / 'logs/macos-service.log')}


def create_launchers():
    PLIST.write_bytes(plistlib.dumps(plist()))
    import shlex
    for title, action in [('FamilyFoto', 'start'), ('Stop FamilyFoto', 'stop')]:
        bundle = ROOT / (title + '.app')
        contents = bundle / 'Contents'
        (contents / 'MacOS').mkdir(parents=True, exist_ok=True)
        (contents / 'Info.plist').write_bytes(plistlib.dumps({
            'CFBundleName':title, 'CFBundleIdentifier':LABEL + '.' + action,
            'CFBundleVersion':'1', 'CFBundlePackageType':'APPL', 'CFBundleExecutable':'launch',
            'LSUIElement':True}))
        executable = contents / 'MacOS/launch'
        command = ' '.join(shlex.quote(str(a)) for a in (PYTHON, Path(__file__).resolve(), action))
        executable.write_text('#!/bin/bash\n' + command + ' >> ' + shlex.quote(str(ROOT / 'logs/macos-launcher.log')) + ' 2>&1\nstatus=$?\nif [ "$status" -ne 0 ]; then\n/usr/bin/osascript -e \'display alert "FamilyFoto could not complete the request" message "See logs/macos-launcher.log in the FamilyFoto folder."\'\nfi\nexit "$status"\n')
        executable.chmod(0o700)


def loaded():
    return subprocess.run(['/bin/launchctl', 'print', DOMAIN + '/' + LABEL], capture_output=True).returncode == 0


def healthy(c):
    try:
        # Loopback health probe of our own HTTPS server; no credentials are sent, so the
        # certificate need not be trusted yet (clients install the CA separately).
        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        with urllib.request.urlopen(f"https://127.0.0.1:{c['port']}/api/macos-health", timeout=1, context=context) as response:
            return json.load(response) == {'instance':LABEL, 'mode':'macos-media'}
    except Exception:
        return False


def start(open_browser=True):
    c, _ = settings()
    validate_ready(c)
    from app.service.macos_lifecycle import backup_running
    if backup_running():
        raise BackupRunning('A backup is running. FamilyFoto starts again by itself when it finishes.')
    if loaded() and healthy(c):
        if open_browser:
            run(['/usr/bin/open', f"https://127.0.0.1:{c['port']}"])
        print_running(c, already=True)
        return
    if loaded():
        # Only unload an exited job, never interrupt a starting or active instance.
        state = subprocess.run(['/bin/launchctl', 'print', DOMAIN + '/' + LABEL], capture_output=True, text=True).stdout
        if 'state = running' in state:
            raise RuntimeError('FamilyFoto is starting or unhealthy; inspect logs before retrying')
        run(['/bin/launchctl', 'bootout', DOMAIN + '/' + LABEL])
    free_ports(c)
    create_launchers()
    run(['/bin/launchctl', 'bootstrap', DOMAIN, PLIST])
    for _ in range(120):
        if healthy(c):
            if open_browser:
                run(['/usr/bin/open', f"https://127.0.0.1:{c['port']}"])
            print_running(c)
            return
        time.sleep(.5)
    raise RuntimeError('FamilyFoto did not start: its web server did not answer in time. '
                       'Inspect logs/macos-service.log and logs/macos-postgres.log')


def print_running(c, already=False):
    """Which server is which: only the web server's address is one to open."""
    print('FamilyFoto is ' + ('already ' if already else '') + 'running.')
    print(f"  Web server: running. Open https://127.0.0.1:{c['port']} in your browser.")
    print(f"  Database server: running (private to FamilyFoto, port {c['database']['port']}; not a web address).")


def stop():
    if not loaded():
        print('FamilyFoto is already stopped: its web server and database server are not running.')
        return
    from app.service.macos_lifecycle import backup_running
    if backup_running():
        raise BackupRunning('A backup is running. Stop FamilyFoto after it finishes.')
    STOP.touch()
    for _ in range(120):
        result = subprocess.run(['/bin/launchctl', 'print', DOMAIN + '/' + LABEL], capture_output=True, text=True)
        if result.returncode or 'state = running' not in result.stdout:
            if not result.returncode:
                run(['/bin/launchctl', 'bootout', DOMAIN + '/' + LABEL])
            STOP.unlink(missing_ok=True)
            print('FamilyFoto stopped cleanly: its web server and database server are stopped.')
            return
        time.sleep(.5)
    raise RuntimeError('Graceful stop still pending; no process was force-terminated')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['install', 'start', 'stop', 'serve', 'status'])
    parser.add_argument('--no-browser', action='store_true', help='Start without opening a browser')
    args = parser.parse_args()
    if sys.platform != 'darwin' or os.geteuid() == 0:
        raise ValueError('Run as your signed-in macOS user, not sudo')
    os.umask(0o077)
    if args.action == 'install':
        initialize()
        create_launchers()
    elif args.action == 'start': start(open_browser=not args.no_browser)
    elif args.action == 'stop': stop()
    elif args.action == 'serve': service()
    else:
        c, _ = settings()
        if loaded() and healthy(c):
            print_running(c, already=True)
        else:
            print('FamilyFoto is stopped, or not answering: its web server is not running at '
                  f"https://127.0.0.1:{c['port']}.")


def cli():
    try:
        main()
    except BackupRunning as error:
        print(error, file=sys.stderr)
        return 1
    except Exception as error:
        print(f'Mac lifecycle failed ({type(error).__name__}). Inspect logs; credentials omitted.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(cli())
