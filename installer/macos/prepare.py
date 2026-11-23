"""Prepare a fresh Mac copy only. Native service/media qualification is separate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
DATABASE_PORT = 5433
WEB_PORT = 4341


def require_fresh(root):
    if os.environ.get("FAMILYFOTO_DATA_DIR"):
        raise ValueError("External data directory overrides are not allowed during fresh setup")
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise ValueError("Linked installation roots are not allowed")
    for name in ('config/local.json', '.venv', 'data', 'logs', 'temp', 'runtime'):
        path = root / name
        if path.exists() or path.is_symlink():
            raise ValueError(f'Existing installation state: {name}. Use a fresh code-only copy.')
    if (root / 'config').is_symlink():
        raise ValueError('Linked configuration directory is not allowed.')
    for name in ('requirements.txt', 'installer/configure.py', 'app/api.py'):
        if not (root / name).is_file():
            raise ValueError('Incomplete application package.')


def require_free_ports(ports):
    # A preflight, not a reservation: startup must recheck before binding later.
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            # Match server rebinding after graceful shutdown; active listeners still conflict.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(('127.0.0.1', port))


def checked_tools(postgres_bin, ffmpeg_bin):
    tools = {name: postgres_bin / name for name in ('initdb', 'pg_ctl', 'postgres')}
    tools.update({name: ffmpeg_bin / name for name in ('ffmpeg', 'ffprobe')})
    for name, path in tools.items():
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f'Missing executable: {name}')
    return tools


def configuration_command(root, tools, database_port=DATABASE_PORT, web_port=WEB_PORT, password_stdin=False, instance=None):
    command = [str(root / '.venv/bin/python3'), str(root / 'installer/configure.py'),
               '--database-port', str(database_port), '--web-port', str(web_port),
               '--ffmpeg', str(tools['ffmpeg']), '--ffprobe', str(tools['ffprobe'])]
    if instance:
        command += ['--instance', instance]
    if password_stdin:
        command.append('--password-stdin')
    return command


def core_dependency_command(python, root):
    """The shared installer's verified local closure when present; otherwise PyPI, as before."""
    if os.environ.get('FAMILYFOTO_PAYLOAD_RECEIPT'):
        return [python, '-m', 'installer.common.install_dependencies', '--role', 'core']
    return [python, '-m', 'pip', 'install', '--disable-pip-version-check',
            '--only-binary=:all:', '-r', str(root / 'requirements.txt')]


def prepare(root, postgres_bin, ffmpeg_bin, database_port=DATABASE_PORT, web_port=WEB_PORT, password_stdin=False,
            password=None, instance=None):
    """`password` sets the administrator password without asking. A restore uses a random one
    nobody sees, because the restored library brings its own accounts."""
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise ValueError('Native Apple Silicon macOS required.')
    if os.geteuid() == 0 or sys.version_info[:2] != (3, 12):
        raise ValueError('Run as the signed-in user using Python 3.12, not sudo.')
    if any(type(p) is not int or not 1024 <= p <= 65535 for p in (database_port, web_port)) or database_port == web_port:
        raise ValueError("Invalid or overlapping ports")
    require_fresh(root)
    tools = checked_tools(postgres_bin, ffmpeg_bin)
    require_free_ports((database_port, web_port))
    os.umask(0o077)
    for name in ('config', 'data', 'logs', 'temp', 'runtime'):
        directory = root / name
        directory.mkdir(exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    receipt_path = root / 'runtime/macos-preparation.json'
    receipt = {'state': 'preparing', 'root': str(root), 'platform': platform.platform(),
               'python': sys.version, 'tools': {name: str(path) for name, path in tools.items()},
               'requirements_sha256': hashlib.sha256((root / 'requirements.txt').read_bytes()).hexdigest(),
               'database_port': database_port, 'web_port': web_port,
               'native_service_tests': 'not-run', 'media_import_tests': 'not-run'}
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    subprocess.run([sys.executable, '-m', 'venv', str(root / '.venv')], check=True, timeout=180)
    python = str(root / '.venv/bin/python3')
    subprocess.run(core_dependency_command(python, root), check=True, timeout=1800, cwd=root)
    subprocess.run([python, '-m', 'pip', 'check'], check=True, timeout=60, cwd=root)
    if password is None:
        subprocess.run(configuration_command(root, tools, database_port, web_port, password_stdin, instance), check=True, cwd=root)
    else:
        subprocess.run(configuration_command(root, tools, database_port, web_port, True, instance), check=True, cwd=root,
                       input=password + '\n', text=True)
    receipt['state'] = 'prepared-not-installed'
    receipt_path.write_text(json.dumps(receipt, indent=2), encoding='utf-8')
    print('Preparation complete. No database server or web server was started. Keep config/local.json private.')
    print('Next: manage.py install sets up the private database server and the launchers; '
          'media qualification is separate.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--postgres-bin', required=True, type=Path)
    parser.add_argument('--ffmpeg-bin', required=True, type=Path)
    args = parser.parse_args()
    try:
        prepare(ROOT, args.postgres_bin, args.ffmpeg_bin)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        # Never print config data or subprocess arguments that might later carry secrets.
        print(f'Preparation stopped ({type(error).__name__}). No services were started. '
              'Do not rerun over partial state; retain logs for review.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
